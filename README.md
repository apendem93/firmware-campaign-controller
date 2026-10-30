# Firmware Campaign Controller

Python/FastAPI, PostgreSQL, a separate scheduler worker, and the **supplied assignment simulator**. Docker Compose runs `scripts/fw_simulator.py`, preserved byte-for-byte from the uploaded `device_simulator.txt`. The controller depends on the published REST/JSON contract, not simulator internals or interviewer-only ground truth.

[The simulator integration guide](docs/simulator-integration.md) explains its functions, profiles, request flow, timing limits, and the corresponding controller code. The older independently written simulator remains an optional regression fixture in `simulator/`; it is no longer the Compose default.

## Run

Requires Docker Engine and Docker Compose v2. `.env.example` documents optional overrides. Defaults bind host ports to loopback and keep PostgreSQL private. The database password is for local development.

```sh
docker compose up --build -d
docker compose ps
```

Compose migrates the database before starting the API, worker, and simulator. Controller port 8000 provides `/docs`, `/healthz`, `/readyz`, and `/metrics`. Simulator port is 8001; worker metrics port is 9001.

Runtime containers use Docker's rotating `local` log driver with a 10 MB file limit and three retained files per container. This bounds local log storage during long simulator runs; `docker compose logs` continues to work. Existing containers need recreation to adopt a changed logging configuration. Campaign and audit records remain in PostgreSQL and are independent of container log retention.

Compose excludes `localhost`, `127.0.0.1`, `api`, `simulator`, and `postgres` from HTTP proxies and preserves additional host `NO_PROXY` / `no_proxy` exclusions. Internal service calls therefore stay on the Compose network even when Docker injects proxy settings. External simulator destinations still follow the configured proxy policy unless explicitly excluded.

For an accelerated run with the raw supplied behavior, start with this configuration instead:

```sh
docker compose -f compose.yaml -f compose.demo.yaml up --build -d
```

The raw simulator has a reproduced defect: its duplicate-inform callback discards commands in the HTTP response. An opt-in simulator adapter consumes those commands, separates periodic traffic from upgrade callbacks, and retries transient callback failures:

```sh
docker compose -f compose.yaml -f compose.demo.yaml -f compose.simulator-fix.yaml up --build -d
```

The adapter leaves the supplied file byte-for-byte unchanged and preserves its device profiles, configured delays, fault probabilities, and firmware behavior. It limits HTTP admission to 16 periodic and 32 critical callbacks by default, using separate connection pools. Transient failures receive up to three total attempts with the same payload. It adds no Download replay or command-key deduplication. The raw two-file command remains available for evaluator parity. See [the callback corrections and their limits](docs/simulator-integration.md#optional-callback-corrections). Use a fresh project/volume when switching scenarios: restarting the simulator resets its fleet, while PostgreSQL retains campaign history.

### Windows PowerShell: port already allocated

If startup reports `Bind for 127.0.0.1:8000 failed: port is already allocated` after switching from the original `firmware-campaign` project to `supplied-demo`, the original stack is still using the host ports. Project names isolate containers, networks, and database volumes; both projects still request ports 8000, 8001, and 9001.

From the repository directory, stop the original stack and resume the new one:

```powershell
docker compose -p firmware-campaign -f compose.yaml -f compose.demo.yaml stop
docker compose -p supplied-demo -f compose.yaml -f compose.demo.yaml -f compose.simulator-fix.yaml up -d --wait
docker compose -p supplied-demo -f compose.yaml -f compose.demo.yaml -f compose.simulator-fix.yaml ps
Invoke-RestMethod http://localhost:8000/readyz
```

These commands preserve both database volumes and reuse the successfully built image. Expect `status: ready`, then open <http://localhost:8000/docs>. Keep `-p supplied-demo` on subsequent Compose commands for this stack; plain `docker compose ps` selects the default `firmware-campaign` project. No volume deletion is needed. For a later source update, use `up --build -d --wait` with the same project and files.

To keep the original stack running instead, use different host ports for the new stack. Set all three variables in PowerShell before the `supplied-demo` startup command:

```powershell
$env:CONTROLLER_PORT = "8010"
$env:SIMULATOR_PORT = "8011"
$env:WORKER_METRICS_PORT = "9011"
docker compose -p supplied-demo -f compose.yaml -f compose.demo.yaml -f compose.simulator-fix.yaml up -d --wait
Invoke-RestMethod http://localhost:8010/readyz
```

The new API is then at <http://localhost:8010/docs>. These variables apply to subsequent Compose commands in that PowerShell session; keep using the same port settings for this stack. Container-to-container addresses stay unchanged.

### Create and inspect a campaign

The demo changes only supported environment settings: download 0–1 seconds, reboot 1–3 seconds, periodic informs every 30 seconds, and late completion delay 60 seconds. Duplicate and late-callback probabilities remain enabled. Its controller result deadline is 120 seconds; reverted devices deliberately wait that deadline before failure classification. The standard profile preserves the supplied defaults (download up to 20 seconds, reboot up to 240 seconds, late delay up to 600 seconds) and uses a 900-second result deadline. Use the standard profile for realistic evaluator timings.

`DEVICE_COUNT` defaults to 1,000 and can be set in `.env` for a fresh fleet; the controller accepts inventory up to 10,000 devices. This is separate from `maxConcurrent`, which limits upgrade slots. The [scale validation report](docs/scale-validation.md) distinguishes measured fleet outcomes, callback delays, resource observations, and remaining limits; do not infer larger-fleet runtime by multiplying a 1,000-device result.

```sh
curl -fsS http://127.0.0.1:8000/campaigns \
  -H 'Content-Type: application/json' \
  -d '{"targetVersion":"2.0.0","imageUrl":"https://firmware.example/2.0.0.bin","deviceFilter":{"currentVersion":"1.0.0"},"maxConcurrent":100,"failureThresholdPercent":100}'
```

The image URL is a placeholder: the supplied simulator models download/reboot delays without fetching images. Supply a real URL when external devices fetch firmware. The 100% threshold permits a full diagnostic run; use a lower threshold for a guarded rollout.

Use the returned ID with `GET /campaigns/{id}`, `POST /campaigns/{id}/pause`, and `POST /campaigns/{id}/resume`. Inspect devices with `GET /campaigns/{id}/devices?state=FAILED&limit=100`, passing `nextCursor` as `cursor` for subsequent pages. Device diagnostics include `faultCode`, `lastBootVersion`, `dispatchedAt`, `resultDeadline`, and `finishedAt` alongside state, reason, and evidence.

Read the persisted audit history using `GET /campaigns/{id}/events?deviceId=GW-...&commandKey=...&after=0&limit=100`. Device/key filters are optional. This endpoint is read-only, returns at most 500 events per page, and orders events by database ID. Pass its `nextCursor` as `after` to read the next page. Payloads show acknowledgment reasons, completion faults, and the transitions used to derive the current outcome.

With Python 3.12 and uv, a runner creates a campaign, checks sampled concurrency and totals, and saves a report:

```sh
uv sync --frozen
uv run python scripts/run_campaign.py --output reports/campaign.json
```

To monitor an existing campaign without creating another, replace `CAMPAIGN_ID` with its ID:

```sh
uv run python scripts/run_campaign.py --campaign-id CAMPAIGN_ID --halt-drain-seconds 120 --output reports/observed-campaign.json
```

`--halt-drain-seconds 120` continues observing already-dispatched work for up to 120 seconds after a halt; it never resumes pending work. Reports distinguish the initial halt snapshot, last observed state, observation age, and stop reason. An observation that ends halted or times out exits nonzero even though the report is saved. Optional `--simulator-summary-url http://127.0.0.1:8001/_admin/summary` adds a diagnostic snapshot after monitoring; it is never used by the controller or runner to decide campaign success.

For a controlled process-restart drill, stop the default stack without deleting its volume, then use a separate project:

```sh
docker compose down
docker compose -p firmware-recovery -f compose.yaml -f compose.demo.yaml -f compose.recovery.yaml up --build -d
uv run python scripts/run_campaign.py --concurrency 40 --restart-after 5 --halt-drain-seconds 120 --compose-project firmware-recovery --compose-file compose.yaml --compose-file compose.demo.yaml --compose-file compose.recovery.yaml --output reports/restart.json
```

`compose.recovery.yaml` selects 40 healthy devices, fixed six-second download and reboot delays, and no duplicate/late callbacks. This separates the planned outage from normal result emission and tests persisted controller state; it supplements the mixed-fleet test rather than replacing it. Use a fresh recovery project for repeatable enrollment. The explicit Compose project/files select the stack being tested; adjust them if you use another project.

This restarts only API and worker, leaving the simulator and database running. This drill uses the raw supplied simulator, which does not retry failed callbacks; a callback emitted while the API is unavailable can be lost permanently. The optional adapter retries transient callback failures, but its attempt limit can be exhausted and a lost response containing the first Download cannot be recovered by replaying the callback. The controller preserves state and handles late evidence, but it cannot reconstruct an unreported result. An unresolved dispatched operation retains its slot and can halt the campaign; that is a truthful recovery outcome, not a reason to resend Download. The runner saves halted results and never automatically resumes a safety halt.

Simulator state is in memory: restarting it resets firmware versions. PostgreSQL's named volume retains campaigns. `docker compose down` preserves that volume; use `down -v` only when intentionally discarding campaign history.

## External simulator

Stop an already-running local simulator with `docker compose stop simulator`, then:

```sh
SIMULATOR_URL=https://your-simulator.example docker compose up --build -d postgres migrate api worker
```

Configure the external simulator's `CONTROLLER_URL` to a reachable controller address. Container-local `localhost` is not another container. External machines require an explicitly configured reachable listener/network route; defaults are loopback-only. Public deployment additionally needs operator authentication and device identity verification.

The optional callback adapter is local test infrastructure; external evaluator integration continues to use the unchanged controller protocol. The controller calls only the two specified simulator APIs. It never calls the supplied `/_admin/summary` endpoint or polls inventory to determine completion. That summary is for evaluator/run-report comparison only. Versions are opaque strings. The supplied simulator sends `startTime` and `completeTime` as Unix epoch seconds, which Pydantic accepts and normalizes. ISO-8601 values are also accepted; timezone-free ISO values are interpreted as UTC. Deadlines use PostgreSQL time rather than device clocks. Inform processing samples that time after acquiring the attempt's campaign lock so lock waits cannot shorten new deadlines or authorize an expired session.

| Owner | Endpoint | Purpose |
|---|---|---|
| Controller | `POST /devices/{deviceId}/inform` | Session, BOOT/completion evidence; returns commands or an empty list. |
| Controller | `POST /devices/{deviceId}/command-response` | ACCEPTED/REJECTED acknowledgment by key. |
| Controller | `POST /campaigns` | Snapshot eligible devices and create campaign. |
| Controller | `GET /campaigns/{id}` | Counts, slots, failure rates, halt reason, sampling epoch, configuration. |
| Controller | `GET /campaigns/{id}/events` | Read-only, filtered, bounded audit-history pagination. |
| Controller | `POST /campaigns/{id}/pause` | Stop new admission and command authorization. |
| Controller | `POST /campaigns/{id}/resume` | Resume or explicitly reset a halted failure-gate sample. |
| Simulator | `GET /devices?version=...` | Fleet discovery at creation. |
| Simulator | `POST /devices/{deviceId}/connection-request` | Nudge; 202 does not establish installation or callback. |

Download is returned within an inform response. TransferComplete arrives in a later inform. Neither has a separate endpoint. ACCEPTED is not upgrade success.

## Architecture and data

```mermaid
flowchart LR
    O[Operator] --> A[FastAPI API]
    S[Simulator] -->|inform and command-response| A
    A -->|commands in inform response| S
    A -->|fleet discovery| S
    A <--> D[(PostgreSQL)]
    W[Scheduler worker] <--> D
    W -->|connection-request| S
```

The API, worker, and simulator are long-lived processes using the application image; the one-shot migration job uses that image too. PostgreSQL is a fourth long-lived service with its own image. PostgreSQL is the durable queue; no Redis/broker is needed. A partial unique index enforces one active campaign. Enrollment snapshots up to 10,000 devices, deduplicates consistent inventory, and completes empty cohorts immediately.

| Table | Data |
|---|---|
| `campaigns` | Settings, tuning snapshot, lifecycle, failure-gate epochs. |
| `devices` | Last observed firmware, event, and receipt time. |
| `campaign_devices` | One attempt per campaign/device, unique key, state, leases, deadlines, acknowledgment, evidence, outcome. |
| `device_events` | Append-only callbacks, transitions, operator actions, nudge results. |

Progress is derived from attempt rows rather than mutable counters. Alembic owns versioned schema changes.

## State machine

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> NOTIFIED: reserve slot
    NOTIFIED --> DOWNLOAD_ISSUED: authorize response
    NOTIFIED --> TIMED_OUT: no session
    NOTIFIED --> PENDING: pause or halt
    DOWNLOAD_ISSUED --> AWAITING_RESULT: accepted or ACK deadline
    DOWNLOAD_ISSUED --> SUCCEEDED: success evidence
    DOWNLOAD_ISSUED --> FAILED: rejection or fault
    AWAITING_RESULT --> SUCCEEDED: target confirmed
    AWAITING_RESULT --> FAILED: failure evidence
    AWAITING_RESULT --> OUTCOME_UNKNOWN: no evidence by deadline
    OUTCOME_UNKNOWN --> SUCCEEDED: late success evidence
    OUTCOME_UNKNOWN --> FAILED: late failure evidence
```

NOTIFIED, DOWNLOAD_ISSUED, AWAITING_RESULT, and OUTCOME_UNKNOWN hold slots. SUCCEEDED, FAILED, and pre-dispatch TIMED_OUT are terminal. DOWNLOAD_ISSUED means durably authorized, not proven delivered.

| Evidence | Outcome |
|---|---|
| Matching zero-fault completion with target firmware | Confirmed success. |
| Matching nonzero-fault completion | Failure with fault code. |
| Zero fault but wrong firmware | Verification grace for BOOT, then VERSION_MISMATCH failure. |
| Target-version BOOT after dispatch, no completion | Success explicitly labeled `boot_inferred`. |
| Old-version BOOT after dispatch, no completion | Wait result deadline, then BOOT_OLD_VERSION failure. A late old BOOT similarly resolves an unknown outcome. |
| No completion/BOOT by dispatch deadline | OUTCOME_UNKNOWN; retain slot and halt dispatch. |
| No session before any Download | TIMED_OUT; release slot. |

BOOT has no key/event timestamp. Its correlation is an inference using one active attempt; stale events cannot be perfectly distinguished. Concurrency is bounded over tracked operations. Stronger physical-device guarantees require a stronger protocol. Periodic version reports alone do not establish success.

Terminal outcomes stay stable. Duplicate keyed callbacks do not double-count or redispatch. The first consistent matching TransferComplete after BOOT-inferred success is classified as `late_completion` and audited as `LATE_CORROBORATION`; subsequent consistent repeats are duplicates. A contradictory completion is classified as `contradiction` and audited as `LATE_TERMINAL_EVIDENCE`. Neither changes the already-recorded outcome; unknown outcomes remain reconcilable. Historical keyed callbacks cannot affect a newer attempt. Unknown completion keys return no commands; cross-device keys return 409; invalid bodies return 422; database failures return 503 rather than uncommitted commands.

## Reliability and controls

Short transactions lock the campaign before admission, dispatch, state changes, and operator decisions. Concurrent workers use `FOR UPDATE SKIP LOCKED`. Outbound calls occur outside locks. Command identity and deadlines commit before the inform response; a crash after commit can still leave delivery uncertain.

Nudges have bounded exponential retries, jitter, and durable leases. Admission reserves only available nudge capacity, so a session deadline does not start for work waiting behind an unbounded dispatch queue. Stale nudge results cannot overwrite callback-driven state. Download is never automatically retransmitted: a unique key does not imply device-side installation deduplication. Missing ACK retains the original result deadline. Duplicate events do not indefinitely extend deadlines.

Pause/halt park unsent reservations. Already authorized responses may arrive afterward; active upgrades continue. Resume from PAUSED retains its sample. Resume from HALTED starts a new sampling epoch and rejects outstanding unknown outcomes. Late evidence does not silently resume dispatch. All-terminal campaigns become COMPLETED even with failures.

Failure rate is `100 * (FAILED + TIMED_OUT) / (SUCCEEDED + FAILED + TIMED_OUT)`, or zero with no resolved devices. Pending, active, and unknown outcomes are excluded. The gate uses strict `>` and minimum `min(minimum_sample, cohort size)`; resumed epochs use the remaining cohort. Overall and epoch rates are exposed. Unknown outcomes independently trigger a safety halt.

Recovery reclaims expired nudge leases, processes deadlines, and reconstructs slots from PostgreSQL. Restarts preserve keys, retries, and timestamps. No in-memory task owns authoritative campaign progress.

## Configuration and observability

Process settings are `DATABASE_URL`, `SIMULATOR_URL`, `CONFIG_FILE`, and `WORKER_METRICS_PORT`. YAML is validated and snapshotted per campaign. Edits affect future campaigns; restart workers for changed loop timing.

| YAML key | Default | Purpose |
|---|---:|---|
| `scheduler_interval` | 1 s | Loop delay. |
| `http_timeout` | 5 s | Outbound timeout. |
| `nudge_attempts` | 3 | Nudge reservations per admission. |
| `nudge_backoff` | 5 s | Initial exponential delay, ±20% jitter. |
| `inform_timeout` | 90 s | Session deadline. |
| `ack_timeout` | 30 s | Missing ACK changes state, never resends. |
| `result_timeout` | 900 s | Upgrade deadline. |
| `verification_grace` | 30 s | Wrong-version/zero-fault grace. |
| `lease_seconds` | 15 s | Must exceed HTTP timeout. |
| `minimum_sample` | 20 | Failure sample before cohort cap. |
| `max_http_concurrency` | 20 | Nudge batch size per worker. |

The supplied simulator settings and profiles are documented in [the integration guide](docs/simulator-integration.md). Fleet identities/profiles are seeded; timing and duplicate/late decisions use a shared random generator, so callback scheduling can change observed results. `.env.example` exposes the supplied settings using their actual names (`SEED`, `P_NO_TC`, `P_REVERT`, and so on). The optional prototype's settings remain in [simulator/README.md](simulator/README.md).

API metrics include incoming counts/latencies by route template, method, and status; outgoing discovery; callback classifications; active state/slot gauges; durable outcomes by evidence; timeout transitions; and recorded nudge retries. Worker metrics include outgoing nudges and heartbeat. Process counters reset on restart; database progress does not. JSON logs carry transition identity/reason. IDs are excluded from metric labels. Responses include `X-Request-ID`. `/readyz` verifies database/schema; `/healthz` checks liveness.

With `compose.simulator-fix.yaml`, simulator port 8001 also exposes `/metrics`: callback attempts by kind/status/outcome, retries and exhausted delivery, queue wait and HTTP duration, and queued/in-flight callbacks by periodic or critical traffic. The raw supplied simulator has no metrics endpoint. `.env.example` exposes the six `SIM_CALLBACK_*` settings forwarded by that override; their defaults and limits are in [the integration guide](docs/simulator-integration.md#optional-callback-corrections). Queue wait is measured separately and has no absolute expiry, so the per-attempt network timeout is not a bound on total callback latency.

## Validation and deliverables

Run tests with a disposable PostgreSQL database:

```sh
docker compose -p firmware-tests -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
docker compose -p firmware-tests -f compose.test.yaml down
```

Or locally:

```sh
uv sync --frozen
TEST_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST:5432/firmware_test uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Fixtures refuse database names other than `firmware_test` because they recreate tables. Tests cover actual PostgreSQL concurrency, callback ordering, deadlines after lock contention, retry leases, threshold boundaries, pause/resume, and callbacks from the unchanged supplied simulator as well as the prototype fixture. Adapter tests exercise transient retries, lost responses, bounded HTTP admission, and separation of periodic and critical traffic. [The run report](docs/run-report.md) contains historical supplied-simulator outcomes and restart evidence; its 1,000-device adapter runs used the earlier duplicate-response-only implementation at commit `6ad4a1c`. [The scale validation report](docs/scale-validation.md) records the later callback investigation and fleet validation. The earlier [prototype run report](docs/archive/local-simulator-run-report.md) and [raw data](docs/archive/local-simulator-run-data.json) are archived separately and are not supplied-simulator results. [The scaling note](docs/scaling.md) discusses 5/10/15 million devices.

Scope trade-offs: one active campaign, no automatic rollback or unsafe Download retry, no full auth platform or dashboards, and a memory-only simulator fleet separate from durable controller state. These choices focus on correctness and recovery within the assignment budget.
