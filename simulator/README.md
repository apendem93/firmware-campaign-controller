# Optional prototype simulator

This independent simulator is a regression fixture for tests and comparative experiments. Docker Compose runs the unchanged supplied simulator at `scripts/fw_simulator.py`; see [the integration guide](../docs/simulator-integration.md).

To run this optional fixture manually, stop the Compose simulator and run `CONTROLLER_URL=http://127.0.0.1:8000 uv run uvicorn simulator.app:app --host 0.0.0.0 --port 8001`. A controller inside Docker must use an address that reaches this host process, such as `http://host.docker.internal:8001` on Docker Desktop. This fixture's retry and deduplication behaviors are stronger than the supplied simulator's and must not be assumed by the controller.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONTROLLER_URL` | `http://api:8000` | Base URL receiving device callbacks. |
| `DEVICE_COUNT` | `1000` | Fleet size, 1–10,000. |
| `SIMULATOR_SEED` | `42` | Deterministic fleet profile seed. |
| `SIMULATOR_TIME_SCALE` | `1` | Multiplier for device delays; `0.02` accelerates demonstration runs. HTTP transport timeouts remain 10 seconds. |

The required API is `GET /devices?version=1.0.0` and `POST /devices/{deviceId}/connection-request`. Every nudge to a known device returns 202, including offline devices: delivery does not establish a callback. `/healthz` and `/summary` are diagnostic additions. The controller must not use `/summary`, or repeatedly query fleet versions, to determine campaign outcomes.

All devices initially run `1.0.0`. Each device's scenario and timing derive independently from a SHA-256 seed, so callback scheduling does not change its assigned behavior. The probability distribution is 70% normal, 5% offline, 5% rejection, 5% transfer fault, 5% target-version BOOT without TransferComplete, 5% old-version BOOT without TransferComplete, 3% duplicated callbacks, and 2% delayed completion following BOOT. Actual counts depend on the seed. Reboot times range from 5 to 240 seconds; online devices send periodic informs every 180–300 seconds. The time multiplier scales device delays, subject to the periodic floor described below.

The simulator models firmware changes and does not fetch or execute image URLs. It retries callback transport errors, 429 responses, and 5xx responses up to five attempts. A delivered callback with a lost response can therefore be duplicated. Download keys are deduplicated locally, but this is not a guarantee the controller can assume from AT&T's contract. Repeated nudges do not independently trigger another download.

State is in memory. Restarting the simulator resets the fleet to `1.0.0`; controller-recovery demonstrations should restart the controller/worker while leaving the simulator running. Task cancellation and HTTP client cleanup happen during shutdown. This implementation is an independent test fixture; the controller only depends on the documented AT&T HTTP interface.

Accelerated runs retain a 30-second minimum periodic-inform interval and allow only one outstanding periodic request per device. This prevents background telemetry from growing an unbounded callback queue. Download/reboot delays still scale normally. Transient callback failures retry up to five times with real-time delays of 1, 2, 4, and 5 seconds, so accelerated device clocks do not collapse the controller restart recovery window.
