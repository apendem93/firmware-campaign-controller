# Supplied simulator validation report

Initially validated on October 26, 2026 with PostgreSQL 16, Python 3.12, Docker Compose, and the supplied simulator; the submission recheck below was performed on October 29. [Raw progress samples, audit checks, operator-control results, and the submission recheck](run-data.json) are retained. The [earlier prototype report](archive/local-simulator-run-report.md) belongs to the original local simulator and is not evidence from this supplied implementation.

## Source integrity and test conditions

`scripts/fw_simulator.py` is byte-for-byte identical to the uploaded `device_simulator.txt`:

```text
SHA-256 b2f0da91e8b9bb7375f46c5e2497d218df588bccdc9b80b196dc3aae95a1c4d8
```

Both mixed-fleet runs used 1,000 devices, seed 42, target 2.0.0, maxConcurrent 100, and failureThresholdPercent 100 to observe the full fleet. Both retained the supplied profile probabilities, 5% duplicate callbacks, and 5% late completions. The supported demo environment used download 0–1 seconds, reboot 1–3 seconds, periodic informs every 30 seconds, and late delay 60 seconds. Controller inform/ACK/result deadlines were 20/10/120 seconds. These are accelerated functional runs, not measurements using the simulator's normal multi-minute timing defaults.

The optional adapter changes only the handling of commands returned to duplicated inform requests. It does not change device profiles, probabilities, firmware changes, busy handling, callback retry behavior, or Download deduplication.

## Observed results

| Run | Status | Succeeded | Failed | Timed out | Unknown | Observation duration |
|---|---|---:|---:|---:|---:|---:|
| Unmodified supplied simulator | HALTED: OUTCOME_UNKNOWN | 844 | 104 | 48 | 4 | 276.574 s, including bounded drain observation |
| Supplied simulator + opt-in duplicate-response adapter | COMPLETED | 848 | 104 | 48 | 0 | 203.555 s |
| Controlled API/worker restart, 40 healthy devices | COMPLETED | 40 | 0 | 0 | 0 | 17.063 s after monitoring started |

The raw run is **not** reported as a successful completed campaign. All devices had been admitted before the safety halt; four unresolved attempts still held slots when the bounded monitor stopped. The controller did not use interviewer ground truth to clear those slots or retry potentially unsafe Downloads.

Seed 42 produced 815 healthy, 33 no-TC, 36 rejecting, 45 transfer-fault, 23 reverting, and 48 offline devices. The corrected run matches that ground truth exactly: 815 + 33 successes, 36 + 45 + 23 failures, and 48 pre-dispatch timeouts. Failure rate was 15.2%. The interviewer summary independently reported 848 devices on target firmware and zero busy devices. It was queried by the reporting harness after monitoring; the controller never calls it.

## Reproduced defect in the supplied simulator

`duplicate_post()` posts a duplicate inform but ignores the controller's response body. A valid sequence is:

1. A device sends PERIODIC while its campaign membership is still PENDING; the response has no command.
2. The scheduler admits that device and reserves a slot.
3. The delayed duplicate PERIODIC arrives. The controller durably authorizes its first Download and returns it.
4. `duplicate_post()` discards that response. No Download executes and no command acknowledgment arrives.
5. The result deadline expires. The controller retains the slot and records OUTCOME_UNKNOWN.

The raw run had four unresolved attempts consistent with this defect, each with a PERIODIC dispatch event and a missing acknowledgment: `GW-1723199DBF2C`, `GW-257F7595CDFB`, `GW-36A2C52B0047`, and `GW-C03987108976`. The recorded events do not establish which simulator handler received each response. A focused test reproduces the exact before/after-admission sequence using the supplied functions. Its paired adapter test verifies the returned command is executed once and the attempt completes.

There is no event/session ID distinguishing the ignored duplicate from a new legitimate session. Reissuing Download in the controller would be unsafe because the supplied simulator does not deduplicate command keys after an operation finishes. The optional adapter repairs the simulator-side response handling by delegating returned Downloads to its original `handle_download()` function. The raw simulator remains the default for evaluator parity.

## Audit and functional verification

- Reconstructing every persisted slot transition showed a maximum of **100** in both 1,000-device runs and **40** in the recovery drill. No negative slot count appeared.
- Duplicate Download authorizations: **0**. Command identities differing from their original authorization: **0**. Pre-dispatch timeouts without a nudge attempt: **0**.
- Both mixed-fleet runs made exactly **one** inventory request from the controller. No version polling or admin-summary dependency drove outcomes.
- Corrected-run failures were 36 explicit rejections, 45 keyed transfer faults, and 23 old-version BOOT inferences. Success evidence was 778 matching completions and 70 BOOT inferences; 37 of those BOOT outcomes later received corroborating completion events. The remaining 33 match the no-TC profile.
- API and worker metrics, scheduler heartbeat, readiness, and the paginated event-history endpoint were exercised successfully. Device IDs were absent from metric labels.
- **85 tests passed**, locally in 17.22 seconds and in the isolated Docker test workflow in 20.36 seconds. This includes the actual supplied implementation, the optional adapter, real PostgreSQL concurrency, migrations, deadlines, callback ordering, diagnostic history, and failure-report handling. Ruff lint/format and Git whitespace checks passed.

## Operator controls and restart recovery

The controlled drill used `compose.recovery.yaml`: 40 healthy devices, six-second download and six-second reboot delays, with duplicate and late injection disabled for this separate scenario. The full mixed-fleet runs above still exercised those behaviors.

Pause returned PAUSED; no new command authorizations appeared during the observation interval. Ordinary resume returned RUNNING without resetting the failure-gate epoch. The runner requested an actual API and worker restart at 6.102 seconds and completed it at 8.966 seconds. PostgreSQL and the supplied simulator remained running. All 40 devices completed under their original command keys.

A second live control test configured 40 rejecting devices and a 0% failure threshold with concurrency five. The campaign halted after the minimum sample of 20 failed outcomes, leaving 20 pending and no active slots. No additional Downloads were authorized during the halted observation. Explicit resume started epoch one; the remaining 20 failures completed the campaign. These rejections are expected test outcomes.

The restart drill verifies durable controller recovery while upgrades are in progress. It does not claim that callbacks emitted during an outage are retried: the supplied simulator drops failed callbacks. Dedicated tests verify that missing ACK alone can still resolve through TransferComplete and that genuinely missing outcome evidence retains its slot safely.

## Submission recheck — October 29, 2026

The isolated Docker/PostgreSQL suite passed again: **85 tests in 17.86 seconds**. The cached runtime/test images' checked application, simulator, test, and dependency files matched the repository. No controller or simulator behavior changed during this recheck.

A fresh 1,000-device accelerated campaign with the optional adapter completed in approximately **186 seconds**: **848 SUCCEEDED, 104 FAILED, 48 TIMED_OUT, zero OUTCOME_UNKNOWN**, with all slots released. The fleet seed, failure probabilities, concurrency 100, and threshold 100 matched the earlier mixed-fleet conditions. Live pause/resume was exercised during this run; the authorization count remained 787 during the paused observation and ordinary resume retained its gate epoch.

Persisted transition reconstruction confirmed maximum slots 100, minimum zero, no duplicate Download authorizations, no changed command identities, and no pre-dispatch timeouts without a nudge attempt. The controller made one inventory request. Metrics, audit pagination, Compose profiles, Ruff, and Alembic schema consistency checks passed. Detailed final observations and checks appear under `submissionRecheck` in `run-data.json`.

The recheck exposed two deployment issues: accumulated logs from an inactive historical cloud demo exhausted the root disk, and Docker-injected HTTP proxies routed internal service discovery through an external proxy. The initial runs affected by those issues are not counted as passing validation. Historical demo containers were removed while preserving their database volume. Compose now configures rotating `local` logs (10 MB, three files per container) and upper/lower `NO_PROXY` exclusions for its service names, preserving host exclusions. Fresh containers were inspected for logging settings, and internal discovery/callback HTTP succeeded with the external proxy still configured. Validation was then rerun successfully.

This adds current local-deployment evidence; it does not extend the earlier raw-simulator, restart, normal-timing, hardware, or scale claims.

## Reproduction

Run each mixed-fleet case with a fresh simulator and no active campaign. The commands below use separate projects sequentially, preserving their database volumes. Stop any other stack occupying ports 8000/8001/9001 first. If these project names have already been used, select new names for a fresh cohort rather than deleting history unintentionally.

```sh
# Exact supplied behavior
docker compose -p assignment-raw -f compose.yaml -f compose.demo.yaml up --build -d
uv sync --frozen
uv run python scripts/run_campaign.py --halt-drain-seconds 150 --simulator-summary-url http://127.0.0.1:8001/_admin/summary --output reports/raw-supplied.json

# Stop the raw stack without deleting its database, then use an independent fleet.
docker compose -p assignment-raw -f compose.yaml -f compose.demo.yaml down
docker compose -p assignment-corrected -f compose.yaml -f compose.demo.yaml -f compose.simulator-fix.yaml up --build -d
uv run python scripts/run_campaign.py --halt-drain-seconds 150 --simulator-summary-url http://127.0.0.1:8001/_admin/summary --output reports/corrected-supplied.json
```

The raw runner intentionally exits nonzero when the campaign remains HALTED. A fixed seed reproduces profiles, but shared asynchronous timing randomness means the number of discarded duplicate responses is not guaranteed to be four on every run. The controlled recovery commands are in the [README](../README.md); the full function mapping and limitations are in the [integration guide](simulator-integration.md).

No 10,000-device load benchmark, normal-timing fleet run, public deployment, or real firmware installation is claimed. The assignment's remaining production evolution concerns are documented in [the scaling note](scaling.md).
