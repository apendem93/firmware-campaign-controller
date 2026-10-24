import asyncio
import json

import httpx
import pytest

from simulator.app import Settings, Simulator, build_fleet, create_app


def test_seed_reproduces_fleet_without_depending_on_fleet_size():
    first = build_fleet(Settings(device_count=1000))
    second = build_fleet(Settings(device_count=1000))
    small = build_fleet(Settings(device_count=1))
    assert first == second
    assert first["device-000001"] == small["device-000001"]
    assert {d.scenario for d in first.values()} == {
        "normal",
        "offline",
        "reject",
        "fault",
        "boot_only",
        "old_boot",
        "duplicate",
        "late",
    }
    assert first != build_fleet(Settings(device_count=1000, seed=43))


async def drain(simulator):
    async def finish():
        while simulator.tasks:
            await asyncio.gather(*list(simulator.tasks))
            await asyncio.sleep(0)

    await asyncio.wait_for(finish(), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "firmware", "ack", "events", "fault"),
    [
        ("normal", "2.0.0", "ACCEPTED", ["TRANSFER_COMPLETE"], 0),
        ("reject", "1.0.0", "REJECTED", [], None),
        ("fault", "1.0.0", "ACCEPTED", ["TRANSFER_COMPLETE"], 9002),
        ("boot_only", "2.0.0", "ACCEPTED", ["BOOT"], None),
        ("old_boot", "1.0.0", "ACCEPTED", ["BOOT"], None),
        ("duplicate", "2.0.0", "ACCEPTED", ["TRANSFER_COMPLETE", "TRANSFER_COMPLETE"], 0),
        ("late", "2.0.0", "ACCEPTED", ["BOOT", "TRANSFER_COMPLETE"], 0),
    ],
)
async def test_upgrade_scenarios(scenario, firmware, ack, events, fault):
    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"commands": []})

    simulator = Simulator(Settings(device_count=1, time_scale=0.0001), httpx.MockTransport(handler))
    await simulator.start(periodic=False)
    try:
        device = simulator.devices["device-000001"]
        device.scenario = scenario
        command = {
            "type": "DOWNLOAD",
            "commandKey": "key-1",
            "targetVersion": "2.0.0",
            "url": "http://firmware/image",
        }
        await simulator.receive_download(device, command)
        await drain(simulator)
        assert device.firmware == firmware
        acknowledgments = [body for path, body in calls if path.endswith("command-response")]
        informs = [body for path, body in calls if path.endswith("inform")]
        assert acknowledgments[0]["status"] == ack
        assert [body["event"] for body in informs] == events
        for body in informs:
            assert body["deviceId"] == device.device_id
            if "transferComplete" in body:
                assert body["transferComplete"]["commandKey"] == "key-1"
                assert body["transferComplete"]["faultCode"] == fault
        assert device.active_key is None
    finally:
        await simulator.stop()


@pytest.mark.asyncio
async def test_duplicate_download_and_nudge_do_not_execute_twice():
    calls = []
    command = {
        "type": "DOWNLOAD",
        "commandKey": "key-1",
        "targetVersion": "2.0.0",
        "url": "http://firmware/image",
    }

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200, json={"commands": [command] if body.get("event") == "CONNECTION_REQUEST" else []}
        )

    simulator = Simulator(Settings(device_count=1, time_scale=0.0001), httpx.MockTransport(handler))
    await simulator.start(periodic=False)
    try:
        device = simulator.devices["device-000001"]
        device.scenario = "normal"
        simulator.nudge(device)
        simulator.nudge(device)
        await drain(simulator)
        await simulator.receive_download(device, command)
        await drain(simulator)
        assert device.downloads_started == 1
        assert sum(c.get("event") == "CONNECTION_REQUEST" for c in calls) == 1
    finally:
        await simulator.stop()


@pytest.mark.asyncio
async def test_callbacks_retry_transient_failure_but_not_validation_failure():
    responses = [503, 200, 422]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(responses.pop(0), json={"commands": []})

    simulator = Simulator(Settings(device_count=1, time_scale=0.0001), httpx.MockTransport(handler))
    await simulator.start(periodic=False)
    try:
        assert await simulator.callback("/test", {}) == {"commands": []}
        assert len(calls) == 2
        assert await simulator.callback("/test", {}) is None
        assert len(calls) == 3
    finally:
        await simulator.stop()


@pytest.mark.asyncio
async def test_http_contract_filters_versions_and_offline_nudge_returns_202():
    app = create_app(Settings(device_count=2))
    simulator = app.state.simulator
    first = simulator.devices["device-000001"]
    first.scenario = "offline"
    simulator.devices["device-000002"].firmware = "2.0.0"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://simulator"
    ) as client:
        response = await client.get("/devices?version=1.0.0")
        assert response.json() == [{"deviceId": first.device_id, "firmwareVersion": "1.0.0"}]
        assert (
            await client.post(f"/devices/{first.device_id}/connection-request")
        ).status_code == 202
        assert not simulator.tasks
        assert (await client.post("/devices/missing/connection-request")).status_code == 404


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_tasks():
    simulator = Simulator(Settings(device_count=1))
    await simulator.start(periodic=False)
    simulator.spawn(asyncio.sleep(1000))
    await simulator.stop()
    assert not simulator.tasks
