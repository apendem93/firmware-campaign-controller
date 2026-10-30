# Archived prototype simulator validation

These results and reproduction commands belong to the original local simulator at revision `1686475`, using Docker Compose, PostgreSQL 16, Python 3.12, and accelerated `config.demo.yaml` settings. The current Compose setup uses the supplied AT&T simulator. Raw progress samples and audit checks are in [local-simulator-run-data.json](local-simulator-run-data.json).

## Reproduction

```sh
docker compose -f compose.yaml -f compose.demo.yaml up --build -d
uv sync --frozen
uv run python scripts/run_campaign.py --restart-after 12 --output reports/campaign-final.json
```

Use a fresh simulator fleet. Preserve PostgreSQL during controller restarts. The local simulator uses seed 42 and 1,000 devices starting on 1.0.0. This campaign targets 2.0.0, with maxConcurrent=100 and failureThresholdPercent=100 to observe the full fleet. Failure-gate behavior is validated separately by integration tests.

## Result

Campaign `a750fc49-4de3-4ed5-928e-4dd3c10e8ae9` reached **COMPLETED** after **133.72 seconds** measured by the runner. Failure rate was **18.70%**. Final held slots: **0**. Halt reason: **None**.

| Device state | Count |
|---|---:|
| PENDING | 0 |
| NOTIFIED | 0 |
| DOWNLOAD_ISSUED | 0 |
| AWAITING_RESULT | 0 |
| OUTCOME_UNKNOWN | 0 |
| SUCCEEDED | 813 |
| FAILED | 147 |
| TIMED_OUT | 40 |

| Outcome | Evidence | Count |
|---|---|---:|
| FAILED | ack | 59 |
| FAILED | boot_inferred | 50 |
| FAILED | transfer_complete | 38 |
| SUCCEEDED | boot_inferred | 70 |
| SUCCEEDED | transfer_complete | 743 |
| TIMED_OUT | deadline | 40 |

## Restart and correctness evidence

- The runner restarted both API and worker at approximately 12.36 seconds; the simulator and PostgreSQL stayed running. This was an actual Compose process restart, not just a database reconnect.
- Reconstructing every slot transition from the durable audit log gives a maximum of 100 and minimum of 0. The configured maximum was never exceeded in that history.
- Duplicate Download authorizations per device: 0. Command identities inconsistent with the original authorization: 0.
- Timeouts with no nudge attempt: 0. Maximum nudge attempts for any device: 3.
- Unknown-outcome transitions in this final run: 0. Dedicated tests verify slot retention, halting, and late reconciliation for this state.
- Both API and worker metrics returned successfully and exposed expected request/outcome/heartbeat series without device-ID labels. API readiness passed.
- **53 tests passed** against PostgreSQL locally and in the isolated Docker test workflow. This includes simultaneous informs/workers, callback ordering, retries, deadlines, failure-gate boundaries, migration round trips, and simulator behaviors. Ruff lint/format and Git whitespace checks passed.

## Findings and limitations

The prototype at this revision bounds outstanding periodic work and applies a 30-second minimum periodic interval in accelerated mode. Callback retry delays use real time. The scheduler reserves only the capacity it can claim in the current nudge batch; a regression test protects this invariant. These changes addressed excessive background callback tasks and reservations without available nudge capacity.

Old-version BOOT outcomes intentionally wait for the result deadline, so the last part of the campaign can show little change while valid timers expire. BOOT-based outcomes remain explicit inferences because the assignment protocol provides no command key/event timestamp on BOOT. Firmware images are simulated, not downloaded or installed.

The simulator's final seeded scenario distribution was `{"boot_only": 53, "duplicate": 37, "fault": 38, "late": 17, "normal": 706, "offline": 40, "old_boot": 50, "reject": 59}`. It observed 901 started downloads and zero active downloads at collection time. This diagnostic endpoint was used only to validate this report; the controller never calls it.

This is a 1,000-device development validation, not a 10,000-device capacity benchmark or a test against AT&T's unpublished simulator. Callback payloads and paths follow the documented external contract. No claim of public deployment, authentication hardening, or real hardware validation is made.
