import json

import httpx
import pytest

from scripts.run_campaign import arguments, run


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def campaign(status="RUNNING", *, slots=1, pending=1, succeeded=0, failed=0, limit=2):
    return {
        "id": "existing",
        "status": status,
        "maxConcurrent": limit,
        "slotsHeld": slots,
        "totalDevices": slots + pending + succeeded + failed,
        "counts": {
            "AWAITING_RESULT": slots,
            "PENDING": pending,
            "SUCCEEDED": succeeded,
            "FAILED": failed,
        },
    }


def execute(tmp_path, handler, *flags, clock=None, restart=None):
    args = arguments(
        ["--campaign-id", "existing", "--output", str(tmp_path / "report.json"), *flags]
    )
    clock = clock or Clock()
    kwargs = {"clock": clock.now, "sleep": clock.sleep}
    if restart:
        kwargs["restart"] = restart
    with httpx.Client(
        base_url=args.controller_url, transport=httpx.MockTransport(handler)
    ) as client:
        report = run(args, client, **kwargs)
    assert json.loads((tmp_path / "report.json").read_text()) == report
    return report


def test_monitor_existing_campaign_never_creates_or_resumes(tmp_path):
    requests = []
    states = [campaign(), campaign("COMPLETED", slots=0, pending=0, succeeded=2)]

    def handler(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=states.pop(0))

    report = execute(tmp_path, handler)
    assert report["successful"] is True
    assert report["stopReason"] == "completed"
    assert requests == [("GET", "/campaigns/existing")] * 2
    assert report["elapsedSeconds"] == 2


def test_halt_drain_retains_first_snapshot_and_never_resumes(tmp_path):
    requests = []
    states = [campaign("HALTED"), campaign("HALTED", slots=0, failed=1)]

    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, json=states.pop(0))

    report = execute(tmp_path, handler, "--halt-drain-seconds", "10")
    assert report["haltSnapshot"]["slotsHeld"] == 1
    assert report["final"]["slotsHeld"] == 0
    assert report["stopReason"] == "halt_drain_finished"
    assert report["successful"] is False
    assert requests == ["GET", "GET"]


def test_unknown_outcomes_cannot_extend_halt_drain_forever(tmp_path):
    clock = Clock()
    report = execute(
        tmp_path,
        lambda request: httpx.Response(200, json=campaign("HALTED")),
        "--halt-drain-seconds",
        "3",
        clock=clock,
    )
    assert report["stopReason"] == "halt_drain_timeout"
    assert report["elapsedSeconds"] == 3
    assert report["finalObservationAgeSeconds"] == 1
    assert report["successful"] is False


def test_default_halt_stops_immediately(tmp_path):
    report = execute(tmp_path, lambda request: httpx.Response(200, json=campaign("HALTED")))
    assert report["stopReason"] == "campaign_halted"
    assert report["elapsedSeconds"] == 0


def test_invariant_violation_is_saved_and_uses_actual_campaign_limit(tmp_path):
    invalid = campaign("COMPLETED", slots=3, pending=0, limit=2)
    report = execute(tmp_path, lambda request: httpx.Response(200, json=invalid))
    assert report["stopReason"] == "invariant_violation"
    assert report["final"] == invalid
    assert report["successful"] is False


def test_stale_creation_response_cannot_claim_monitor_success(tmp_path):
    clock = Clock()
    args = arguments(["--timeout", "3", "--output", str(tmp_path / "report.json")])

    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json=campaign("COMPLETED", slots=0, pending=0))
        return httpx.Response(503)

    with httpx.Client(
        base_url=args.controller_url, transport=httpx.MockTransport(handler)
    ) as client:
        report = run(args, client, clock=clock.now, sleep=clock.sleep)
    assert report["final"]["status"] == "COMPLETED"
    assert report["stopReason"] == "monitor_timeout"
    assert report["successful"] is False
    assert report["statusRequestFailures"] == 3
    assert report["finalObservationAgeSeconds"] == 3
    assert report["lastObservedAt"] is not None
    assert json.loads((tmp_path / "report.json").read_text()) == report


def test_summary_is_only_read_after_monitoring_and_cannot_replace_campaign_state(tmp_path):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/_admin/summary":
            return httpx.Response(200, json={"versions": {"2.0.0": 1000}, "busy": 0})
        return httpx.Response(200, json=campaign("HALTED"))

    report = execute(
        tmp_path, handler, "--simulator-summary-url", "http://simulator/_admin/summary"
    )
    assert paths == ["/campaigns/existing", "/_admin/summary"]
    assert report["final"]["status"] == "HALTED"
    assert report["successful"] is False
    assert report["simulatorSummary"]["busy"] == 0


def test_summary_failure_is_reported_separately(tmp_path):
    def handler(request):
        if request.url.path == "/_admin/summary":
            return httpx.Response(503)
        return httpx.Response(200, json=campaign("COMPLETED", slots=0, pending=0, succeeded=2))

    report = execute(
        tmp_path, handler, "--simulator-summary-url", "http://simulator/_admin/summary"
    )
    assert report["successful"] is True
    assert report["simulatorSummaryError"] == "HTTPStatusError"


def test_restart_honors_explicit_project_and_override_files(tmp_path):
    calls = []

    def restart(command, **kwargs):
        calls.append((command, kwargs))

    report = execute(
        tmp_path,
        lambda request: httpx.Response(200, json=campaign("COMPLETED", slots=0, pending=0)),
        "--controller-url",
        "http://localhost:8010",
        "--restart-after",
        "0",
        "--compose-project",
        "isolated",
        "--compose-file",
        "compose.yaml",
        "--compose-file",
        "compose.demo.yaml",
        restart=restart,
    )
    assert calls[0][0] == [
        "docker",
        "compose",
        "--project-name",
        "isolated",
        "--file",
        "compose.yaml",
        "--file",
        "compose.demo.yaml",
        "restart",
        "api",
        "worker",
    ]
    assert calls[0][1]["check"] is True
    assert report["restart"]["completedAtSeconds"] == 0


def test_non_default_controller_requires_explicit_restart_project():
    with pytest.raises(SystemExit):
        arguments(["--controller-url", "http://localhost:8010", "--restart-after", "10"])


def test_response_arriving_after_monitor_deadline_is_not_reported_as_timely_success(tmp_path):
    clock = Clock()

    def handler(request):
        clock.sleep(4)
        return httpx.Response(200, json=campaign("COMPLETED", slots=0, pending=0))

    report = execute(tmp_path, handler, "--timeout", "3", clock=clock)
    assert report["stopReason"] == "monitor_timeout"
    assert report["successful"] is False
