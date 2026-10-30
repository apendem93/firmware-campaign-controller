# Callback reliability and fleet validation

This report separates the simulator's callback bottleneck from controller state-machine correctness. The supplied `scripts/fw_simulator.py` remains byte-for-byte unchanged; the corrections are selected explicitly with `compose.simulator-fix.yaml`.

[Recorded evidence](scale-run-data.json) includes the failed baselines, diagnostic profiles, source hashes, completed-run progress samples, persisted audits, and resource observations. Profiling and synthetic experiments are kept separate from full campaign measurements.

## Investigation

At revision `6ad4a1c`, the accelerated 2,000-device campaign halted after 158.183 seconds with one unresolved upgrade. The simulator logged a callback `ReadError` for that device; an accepted Download had no subsequent qualifying BOOT or TransferComplete. Later PERIODIC reports were not used to manufacture success. The exact origin of the connection error was not established.

The earlier 5,000-device campaign halted after 124.685 seconds. Five attempts exceeded the 120-second result deadline; all five subsequently resolved through BOOT or TransferComplete. Their acknowledgments arrived approximately 62–147 seconds after command authorization. The campaign remained halted pending explicit operator resume, with 4,714 devices pending. Neither elapsed time is a full-campaign completion benchmark.

Controller logs from that 5,000-device run showed:

| Callback | Requests | Mean API processing | p99 | Maximum |
|---|---:|---:|---:|---:|
| Inform | 24,186 | 43 ms | 257 ms | 597 ms |
| Command response | 263 | 61 ms | 385 ms | 502 ms |

These instrumented API-processing times did not account for the multi-minute acknowledgment delays; they exclude client-side waiting and any server queueing before request instrumentation. The simulator used approximately one CPU core while its shared HTTPX client admitted unbounded callback tasks into a pool with 500 connection slots. Periodic informs, acknowledgments, and results competed in that pool. Its download handler waits for the ACK request before starting the download/reboot delays.

A diagnostic profile of the actual 5,000-device simulator attributed **89.505 of 98.586 recorded seconds (90.8%)** to HTTPcore's `_assign_requests_to_connections()`. That function repeatedly scans connections and queued requests, including nested idle-connection checks. The resulting CPU work delayed the simulator's event loop and callback delivery. This was a software-induced single-process CPU bottleneck; the observations did not show memory exhaustion or establish a need for a larger machine.

Profiling adds overhead and changes scheduling. These diagnostic durations are not campaign runtimes. A separate synthetic HTTP-only experiment also showed that simply increasing idle connections could make performance worse; small admission limits trade background queue delay for bounded pool work. Successful fleet runs below are the relevant end-to-end evidence.

## Corrections

- Admit HTTP requests through semaphores before entering HTTPX. Periodic traffic uses its own 16-connection pool; ACK, BOOT, TransferComplete, and connection-request informs use a separate 32-connection pool.
- Retry transient transport failures, HTTP 429, and HTTP 5xx with the same callback body and command identity. Defaults are three total attempts, five seconds per network attempt, exponential backoff with jitter, and a two-second maximum retry delay. An excessive `Retry-After` stops delivery rather than retrying early.
- Consume commands returned to duplicate informs, as in the earlier adapter. Callback retries never replay Download or fabricate completion evidence.
- Expose simulator callback attempts, queue wait, network latency, retries, exhaustion, and current waiting/in-flight work at `/metrics`.
- Sample the controller's database clock after acquiring the campaign lock. A request waiting on that lock can no longer authorize an expired session or start a new command with shortened deadlines.

The raw simulator remains available. Its API contract, device profiles, seed, configured firmware delays, fault probabilities, and original source are preserved. Semaphore waiting time has no absolute expiry; the network timeout is not a bound on total callback latency. A lost response containing the first Download remains an unresolved protocol case. BOOT remains unkeyed, and long outages can still exhaust callback retries. The controller retains its safety halt and does not blindly reissue commands.

## Validation conditions

The new fleet runs use fresh, independent PostgreSQL volumes and simulator processes, sequentially on the same shared Docker host as the failed baseline tests. No CPU or memory allocation was increased. Docker reports five CPUs and approximately 33.29 GiB host memory; this is host capacity, not a requirement or a dedicated per-container allocation. A separate historical demo with no active campaign remained running; its periodic callbacks continued. CPU percentages in resource samples use 100% to represent approximately one core.

- Python 3.12.16, PostgreSQL 16, frozen dependency versions; normal runtime images, without profiling.
- Seed 42; target `2.0.0`; source `1.0.0`; `maxConcurrent: 100`; `failureThresholdPercent: 100`.
- The same accelerated profile: download 0–1 seconds, reboot 1–3 seconds, periodic informs every 30 seconds, late delay 60 seconds. Duplicate and late probabilities remain 5% each.
- Controller inform/ACK/result deadlines remain 20/10/120 seconds. No deadline was extended to make a run pass.
- Campaign duration excludes image build and service startup. The runner waits two seconds between status requests; request processing adds to the interval. Database creation-to-completion duration is recorded separately.
- The cloud build supplied its corporate proxy CA as a temporary BuildKit secret for dependency downloads. TLS verification stayed enabled; the repository Dockerfile and dependency lock were unchanged.

The full Docker/PostgreSQL suite passed **108 tests in 15.49 seconds**, including response loss after committed ACK/BOOT/TransferComplete, permanent HTTP failures, bounded transient retries, priority isolation, cancellation, lost Download responses, and campaign-lock deadline contention. Ruff and formatting checks passed. Runtime image contents matched all 26 checked Python/dependency files.

## Fleet results

| Fleet | Status | Succeeded | Failed | Timed out | Unknown | Observed duration |
|---|---|---:|---:|---:|---:|---:|
| 5,000 | COMPLETED | 4,308 | 456 | 236 | 0 | 891.899 s (14 min 52 s) |
| 10,000 | COMPLETED | 8,601 | 899 | 500 | 0 | 2,142.149 s (35 min 42 s) |

Both audits reconstructed zero negative slots, zero duplicate Download authorizations, unchanged command identities, zero unknown transitions, and zero halt events. Every enrolled attempt reached a terminal state. The persisted-transition audits found slot maxima of 100 and 79 respectively, both within the configured limit of 100; the limit is a ceiling, not a promise to keep every slot occupied. All slots were released at completion. Each controller made one inventory request and reported no HTTP 5xx responses. No callback delivery attempts failed or exhausted their retries during either run; injected response-loss behavior is covered separately by tests. The 10,000-device services had zero restarts and no OOM kills.

All 4,764 and 9,500 dispatched attempts respectively had a recorded ACK. The database creation-to-completion durations were 891.467 and 2,140.327 seconds; the table above uses the reporting runner's observed duration.

| Delivery measurement | 5,000 devices | 10,000 devices |
|---|---:|---:|
| Authorization to first ACK, mean | 0.477 s | 0.585 s |
| Authorization to first ACK, exact database p99 | 0.899 s | 1.001 s |
| Authorization to first ACK, maximum | 2.449 s | 3.251 s |
| Critical admission wait, mean | 0.172 ms | 0.207 ms |
| Critical admission wait, p99 bucket upper bound | 5 ms | 5 ms |
| Periodic admission wait, mean | 0.393 s | 26.323 s |
| Periodic admission wait, p99 bucket upper bound | 5 s | 60 s |

Histogram bucket bounds are not exact percentiles. The separate queues allowed bulk traffic to wait without filling the critical HTTP pool. At 10,000 devices the simulator could not sustain a completed periodic request every 30 seconds for every device: queue waiting extended the effective interval. The simulator's configured loop delay remained 30 seconds, and upgrade callbacks continued promptly.

The simulator's diagnostic profiles matched the final controller counts exactly:

- **5,000:** 4,141 healthy plus 167 no-TC successes; 153 rejections, 213 transfer faults, and 90 old-version BOOT failures; 236 offline timeouts.
- **10,000:** 8,266 healthy plus 335 no-TC successes; 292 rejections, 414 transfer faults, and 193 old-version BOOT failures; 500 offline timeouts.

Diagnostic ground truth was fetched only after monitoring and never used by the controller to choose outcomes. Both summaries reported zero busy devices after monitoring.

## Hardware versus code

| Container | Mean CPU, 5,000 | Mean CPU, 10,000 | Maximum sampled memory, 5,000 / 10,000 |
|---|---:|---:|---:|
| API | 103.5% | 106.9% | 103.3 / 112.9 MiB |
| Worker | 9.2% | 7.0% | 73.5 / 76.5 MiB |
| Simulator | 48.1% | 50.7% | 72.2 / 87.4 MiB |
| PostgreSQL | 36.8% | 41.1% | 143.0 / 156.4 MiB |

There were 85 and 204 resource samples respectively, with zero collection errors. These are Docker container measurements, including their processes and health checks; 100% corresponds approximately to one CPU core. Memory maxima are sampled observations, not minimum RAM requirements or guaranteed continuous peaks.

The primary failure mechanism was **simulator software and load management**: unbounded shared HTTP pooling consumed an event-loop core and delayed upgrade callbacks. The raw sender also discarded failed callbacks without retrying. Corrected runs completed on the same host without adding CPU or RAM and without extending controller deadlines. That supports a code-level diagnosis for the observed failure, not a hard fleet-size or memory ceiling at 1,000 devices.

The API container became the busiest component, and the longer 10,000-device runtime shows the remaining throughput cost of background reporting and the current controller design. One API process, serialized campaign coordination, bounded nudge batches, and repeated database work remain architectural trade-offs. This investigation does not isolate their individual cost or claim unlimited scaling. A larger or faster machine can change runtime; it is not the correction that made these campaigns complete.

## Reproduction

Use the base, accelerated demo, and callback-correction Compose files. For each run, start with a fresh project name and keep concurrency at 100. Stop any previous stack occupying the selected ports first; preserve its database volume.

Use a fresh checkout and a clean `.env` containing the entries below, so earlier fault/profile overrides cannot change the experiment. Clear conflicting shell variables as well; shell values take precedence. For a terminal-independent configuration:

```text
COMPOSE_PROJECT_NAME=firmware-review-5000
COMPOSE_PATH_SEPARATOR=,
COMPOSE_FILE=compose.yaml,compose.demo.yaml,compose.simulator-fix.yaml
DEVICE_COUNT=5000
```

Then run:

```sh
docker compose up --build -d --wait
```

Create the campaign in Swagger UI at `http://localhost:8000/docs` using the README's request body, or use the reporting runner with Python 3.12 and uv:

```sh
uv sync --frozen
uv run python scripts/run_campaign.py --concurrency 100 --failure-threshold 100 --timeout 2400 --halt-drain-seconds 150 --simulator-summary-url http://127.0.0.1:8001/_admin/summary --output reports/5000.json
docker compose down
```

For the second run, change the project name to a new value such as `firmware-review-10000`, set `DEVICE_COUNT=10000`, and repeat with output `reports/10000.json` and `--timeout 3600` for monitoring headroom. Shell environment variables override `.env`; use a fresh terminal or clear conflicting settings. Do not reuse a completed simulator's firmware inventory as a fresh cohort. The runner records halted/failed observations and never automatically resumes a safety halt.

## Limits of the conclusion

The corrected callback transport addresses the observed simulator bottleneck and transient delivery loss. The earlier `ReadError` connection-close cause remains unproven; recovery from that failure class is covered by fault-injection tests. The controller's single campaign-row lock, repeated aggregate queries, single API process, and PostgreSQL write workload remain throughput limits. CPU saturation of a process does not by itself mean state-machine failure, and additional hardware does not remove an inefficient queueing algorithm.

Results with this opt-in adapter must not be presented as results from the unmodified external evaluator's simulator. Accelerated local campaigns do not establish normal-timing firmware performance, public-cloud capacity, real-device installation behavior, or national-scale readiness. The [national-scale design note](scaling.md) discusses the separate architectural changes for millions of devices.
