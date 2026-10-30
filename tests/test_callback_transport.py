import asyncio
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from simulator import callback_transport
from simulator.callback_transport import CallbackSettings, CallbackTransport


def tuning(**kwargs):
    return CallbackSettings(retry_base_delay=0.001, max_retry_delay=0.001, **kwargs)


@pytest.mark.parametrize("failure", ["read_error", "timeout", 429, 503])
async def test_transient_failure_retries_identical_callback(failure):
    bodies = []
    body = {
        "deviceId": "GW-TEST",
        "event": "TRANSFER_COMPLETE",
        "transferComplete": {"commandKey": "key", "faultCode": 0, "completeTime": 123},
    }

    async def deliver(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            body["transferComplete"]["completeTime"] = 999
            if failure == "read_error":
                raise httpx.ReadError("connection closed", request=request)
            if failure == "timeout":
                await asyncio.sleep(1)
            return httpx.Response(failure, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"commands": []})

    async with CallbackTransport(
        "http://controller",
        tuning(request_timeout=0.01),
        transport=httpx.MockTransport(deliver),
    ) as callbacks:
        assert await callbacks.send("/devices/GW-TEST/inform", body) == {"commands": []}
        assert (
            callbacks.registry.get_sample_value(
                "simulator_callback_retries_total", {"kind": "transfer_complete"}
            )
            == 1
        )
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert bodies[1]["transferComplete"]["completeTime"] == 123


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422])
async def test_permanent_http_error_is_not_retried(status):
    calls = []

    def deliver(request):
        calls.append(request)
        return httpx.Response(status)

    async with CallbackTransport(
        "http://controller", tuning(), transport=httpx.MockTransport(deliver)
    ) as callbacks:
        assert (
            await callbacks.send("/devices/GW-TEST/command-response", {"commandKey": "key"}) is None
        )
        assert (
            callbacks.registry.get_sample_value(
                "simulator_callback_exhausted_total", {"kind": "ack"}
            )
            == 1
        )
    assert len(calls) == 1


async def test_retry_attempts_are_bounded_and_do_not_leak_permits():
    calls = []

    def deliver(request):
        calls.append(request)
        raise httpx.ReadError("disconnected", request=request)

    async with CallbackTransport(
        "http://controller", tuning(max_attempts=3), transport=httpx.MockTransport(deliver)
    ) as callbacks:
        assert await callbacks.send("/devices/GW-TEST/inform", {"event": "BOOT"}) is None
        assert (
            callbacks.registry.get_sample_value(
                "simulator_callback_inflight", {"traffic": "critical"}
            )
            == 0
        )
        assert (
            callbacks.registry.get_sample_value(
                "simulator_callback_retries_total", {"kind": "boot"}
            )
            == 2
        )
    assert len(calls) == 3


async def test_critical_callback_bypasses_saturated_periodic_queue():
    periodic_entered = asyncio.Event()
    release = asyncio.Event()
    active = maximum = 0

    async def deliver(request):
        nonlocal active, maximum
        body = json.loads(request.content)
        if body.get("event") == "PERIODIC":
            active += 1
            maximum = max(maximum, active)
            periodic_entered.set()
            try:
                await release.wait()
            finally:
                active -= 1
        return httpx.Response(200, json={"commands": []})

    async with CallbackTransport(
        "http://controller",
        tuning(periodic_concurrency=2, critical_concurrency=1),
        transport=httpx.MockTransport(deliver),
    ) as callbacks:
        periodic = [
            asyncio.create_task(callbacks.send(f"/devices/GW-{i}/inform", {"event": "PERIODIC"}))
            for i in range(20)
        ]
        try:
            await asyncio.wait_for(periodic_entered.wait(), 1)
            response = await asyncio.wait_for(
                callbacks.send(
                    "/devices/GW-TEST/command-response", {"commandKey": "key", "status": "ACCEPTED"}
                ),
                1,
            )
            assert response == {"commands": []}
            assert not release.is_set()
            release.set()
            await asyncio.gather(*periodic)
        finally:
            for task in periodic:
                task.cancel()
            await asyncio.gather(*periodic, return_exceptions=True)
        assert maximum == 2
        assert (
            callbacks.registry.get_sample_value(
                "simulator_callback_queued", {"traffic": "periodic"}
            )
            == 0
        )


async def test_cancellation_cleans_waiting_and_active_callbacks():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def deliver(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={})

    async with CallbackTransport(
        "http://controller",
        tuning(periodic_concurrency=1),
        transport=httpx.MockTransport(deliver),
    ) as callbacks:
        first = asyncio.create_task(callbacks.send("/devices/first/inform", {"event": "PERIODIC"}))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(
            callbacks.send("/devices/second/inform", {"event": "PERIODIC"})
        )
        await asyncio.sleep(0)
        second.cancel()
        first.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        for metric in ("simulator_callback_queued", "simulator_callback_inflight"):
            assert callbacks.registry.get_sample_value(metric, {"traffic": "periodic"}) == 0
        release.set()
        assert (
            await asyncio.wait_for(
                callbacks.send("/devices/third/inform", {"event": "PERIODIC"}), 1
            )
            == {}
        )


def test_callback_settings_reject_unbounded_or_invalid_configuration(monkeypatch):
    monkeypatch.setenv("SIM_CALLBACK_CRITICAL_CONCURRENCY", "0")
    with pytest.raises(ValidationError):
        CallbackSettings()


@pytest.mark.parametrize("header_kind", ["seconds", "http_date"])
async def test_retry_after_is_honored_without_shortening_server_delay(monkeypatch, header_kind):
    header = "0.02"
    if header_kind == "http_date":
        deadline = datetime(2015, 10, 21, 7, 28, tzinfo=UTC).timestamp()
        monkeypatch.setattr(callback_transport.time, "time", lambda: deadline - 0.02)
        header = "Wed, 21 Oct 2015 07:28:00 GMT"
    request_times = []

    def deliver(request):
        request_times.append(time.monotonic())
        if len(request_times) == 1:
            return httpx.Response(429, headers={"Retry-After": header})
        return httpx.Response(200, json={})

    settings = CallbackSettings(retry_base_delay=0.001, max_retry_delay=0.1)
    async with CallbackTransport(
        "http://controller", settings, transport=httpx.MockTransport(deliver)
    ) as callbacks:
        assert await callbacks.send("/devices/GW-TEST/command-response", {}) == {}
    assert len(request_times) == 2
    assert request_times[1] - request_times[0] >= 0.02


@pytest.mark.parametrize("header_kind", ["seconds", "http_date"])
async def test_retry_after_beyond_local_budget_stops_instead_of_retrying_early(
    monkeypatch, caplog, header_kind
):
    header = "60"
    if header_kind == "http_date":
        deadline = datetime(2015, 10, 21, 7, 28, tzinfo=UTC).timestamp()
        monkeypatch.setattr(callback_transport.time, "time", lambda: deadline - 60)
        header = "Wed, 21 Oct 2015 07:28:00 GMT"
    calls = []

    def deliver(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": header})

    async with CallbackTransport(
        "http://controller", CallbackSettings(), transport=httpx.MockTransport(deliver)
    ) as callbacks:
        assert await callbacks.send("/devices/GW-TEST/command-response", {}) is None
        assert (
            callbacks.registry.get_sample_value("simulator_callback_retries_total", {"kind": "ack"})
            is None
        )
        assert (
            callbacks.registry.get_sample_value(
                "simulator_callback_exhausted_total", {"kind": "ack"}
            )
            == 1
        )
    assert len(calls) == 1
    assert "Retry-After 60.000s exceeds retry budget 2.000s; not retrying early" in caplog.text
