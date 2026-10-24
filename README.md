# Firmware Campaign Controller

Python/FastAPI, PostgreSQL, a separate scheduler worker, and an independent seeded simulator. The controller implements the assignment REST/JSON contract; AT&T's simulator can replace the local simulator without controller changes.

## Run

Requires Docker Engine and Docker Compose v2. `.env.example` documents optional overrides. Defaults bind host ports to loopback and keep PostgreSQL private. The database password is for local development.

```sh
docker compose up --build -d
docker compose ps
```

Compose migrates the database before starting the API, worker, and simulator. Controller port 8000 provides `/docs`, `/healthz`, `/readyz`, and `/metrics`. Simulator port is 8001; worker metrics port is 9001.

For an accelerated local demo, start with this configuration instead:

```sh
docker compose -f compose.yaml -f compose.demo.yaml up --build -d
```

The demo shortens deadlines and sets simulator time scale to 0.02. Use normal configuration for external simulators whose reboots can take minutes.

```sh
curl -fsS http://127.0.0.1:8000/campaigns \
  -H 'Content-Type: application/json' \
  -d '{"targetVersion":"2.0.0","imageUrl":"https://firmware.example/2.0.0.bin","deviceFilter":{"currentVersion":"1.0.0"},"maxConcurrent":100,"failureThresholdPercent":100}'
```

The image URL is a placeholder: the local simulator models downloads without fetching images. Supply a real URL when external devices fetch firmware. The 100% threshold permits a full diagnostic run; use a lower threshold for a guarded rollout.

Use the returned ID with `GET /campaigns/{id}`, `POST /campaigns/{id}/pause`, and `POST /campaigns/{id}/resume`. Inspect devices with `GET /campaigns/{id}/devices?state=FAILED&limit=100`, passing `nextCursor` as `cursor` for subsequent pages.

With Python 3.12 and uv, a runner creates a campaign, checks sampled concurrency and totals, and saves a report:

```sh
uv sync --frozen
uv run python scripts/run_campaign.py --output reports/campaign.json
```

For a process-restart exercise on a fresh local fleet:

```sh
uv run python scripts/run_campaign.py --restart-after 12 --output reports/restart.json
```

This restarts only API and worker, leaving the simulator and database running. The runner saves halted results and never automatically resumes a safety halt. Simulator state is in memory: restarting it resets firmware versions. PostgreSQL's named volume retains campaigns. `docker compose down` preserves that volume; use `down -v` only when intentionally discarding campaign history.

## External simulator

Stop an already-running local simulator with `docker compose stop simulator`, then:

```sh
SIMULATOR_URL=https://your-simulator.example docker compose up --build -d postgres migrate api worker
```

Configure the external simulator's `CONTROLLER_URL` to a reachable controller address. Container-local `localhost` is not another container. External machines require an explicitly configured reachable listener/network route; defaults are loopback-only. Public deployment additionally needs operator authentication and device identity verification.

The controller calls only the two specified simulator APIs. It never uses local `/summary`, nor polls fleet versions to determine completion. Versions are opaque strings. ISO-8601 completion times without a timezone are interpreted as UTC. Scheduler deadlines use PostgreSQL receipt time.

| Owner | Endpoint | Purpose |
|---|---|---|
| Controller | `POST /devices/{deviceId}/inform` | Session, BOOT/completion evidence; returns commands or an empty list. |
| Controller | `POST /devices/{deviceId}/command-response` | ACCEPTED/REJECTED acknowledgment by key. |
| Controller | `POST /campaigns` | Snapshot eligible devices and create campaign. |
| Controller | `GET /campaigns/{id}` | Counts, slots, failure rates, halt reason, sampling epoch, configuration. |
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

Four long-lived services plus a migration job share one image. PostgreSQL is the durable queue; no Redis/broker is needed. A partial unique index enforces one active campaign. Enrollment snapshots up to 10,000 devices, deduplicates consistent inventory, and completes empty cohorts immediately.

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

Terminal outcomes stay stable. Duplicate callbacks do not double-count or redispatch. Late contradictions are audited; unknown outcomes remain reconcilable. Historical keyed callbacks cannot affect a newer attempt. Unknown completion keys return no commands; cross-device keys return 409; invalid bodies return 422; database failures return 503 rather than uncommitted commands.

## Reliability and controls

Short transactions lock the campaign before admission, dispatch, state changes, and operator decisions. Concurrent workers use `FOR UPDATE SKIP LOCKED`. Outbound calls occur outside locks. Command identity and deadlines commit before the inform response; a crash after commit can still leave delivery uncertain.

Nudges have bounded exponential retries, jitter, and durable leases. Stale nudge results cannot overwrite callback-driven state. Download is never automatically retransmitted: a unique key does not imply device-side installation deduplication. Missing ACK retains the original result deadline. Duplicate events do not indefinitely extend deadlines.

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

Simulator settings and profiles are in [simulator/README.md](simulator/README.md). Profiles are deterministic per seed; scheduling and aggressive deadlines can affect actual results.

API metrics include incoming counts/latencies by route template, method, and status; outgoing discovery; callback classifications; active state/slot gauges; durable outcomes by evidence; timeout transitions; and recorded nudge retries. Worker metrics include outgoing nudges and heartbeat. Process counters reset on restart; database progress does not. JSON logs carry transition identity/reason. IDs are excluded from metric labels. Responses include `X-Request-ID`. `/readyz` verifies database/schema; `/healthz` checks liveness.

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

Fixtures refuse database names other than `firmware_test` because they recreate tables. Tests cover actual PostgreSQL concurrency, callback ordering, deadlines, retry leases, threshold boundaries, pause/resume, and simulator behaviors. [The run report](docs/run-report.md) contains measured outcomes and restart evidence. [The scaling note](docs/scaling.md) discusses 5/10/15 million devices.

Scope trade-offs: one active campaign, no automatic rollback or unsafe Download retry, no full auth platform or dashboards, and a memory-only simulator fleet separate from durable controller state. These choices focus on correctness and recovery within the assignment budget.
