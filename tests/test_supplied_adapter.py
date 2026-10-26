import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_supplied_simulator import attempt, authorizations, drain, enroll, schedule
from test_supplied_simulator import supplied_fleet as supplied_fleet

from scripts import fw_simulator as reference
from simulator import supplied_adapter as adapter


@pytest.mark.integration
async def test_adapter_executes_download_returned_only_to_duplicate(
    client, database, supplied_fleet, monkeypatch
):
    monkeypatch.setattr(reference, "P_DUPLICATE", 1)
    monkeypatch.setattr(reference, "duplicate_post", adapter.duplicate_post)
    supplied_fleet.duplicate_release = asyncio.Event()
    campaign_id = await enroll(client, database)
    await reference.inform(supplied_fleet.device, "PERIODIC")
    assert supplied_fleet.calls[0]["response"] == {"commands": []}
    await schedule(database)
    supplied_fleet.duplicate_release.set()
    await drain()
    assert len(supplied_fleet.calls[1]["response"]["commands"]) == 1
    assert (await attempt(database)).state == "SUCCEEDED"
    assert supplied_fleet.device["version"] == "2.0.0"
    assert await authorizations(database) == 1
    status = (await client.get(f"/campaigns/{campaign_id}")).json()
    assert status["status"] == "COMPLETED"
    assert status["slotsHeld"] == 0


@pytest.mark.integration
async def test_adapter_true_duplicates_do_not_execute_another_download(
    client, database, supplied_fleet, monkeypatch
):
    monkeypatch.setattr(reference, "P_DUPLICATE", 1)
    monkeypatch.setattr(reference, "duplicate_post", adapter.duplicate_post)
    executions = []
    original = reference.handle_download

    async def track(device, command):
        executions.append(command["commandKey"])
        await original(device, command)

    monkeypatch.setattr(reference, "handle_download", track)
    await enroll(client, database)
    await schedule(database)
    await reference.inform(supplied_fleet.device, "CONNECTION_REQUEST")
    await drain()
    assert len(executions) == 1
    assert await authorizations(database) == 1
    assert (await attempt(database)).state == "SUCCEEDED"


async def test_adapter_does_not_execute_commands_from_ack_response(monkeypatch):
    calls = []
    device = {"id": "GW-TEST"}

    async def forbidden(device, command):
        calls.append(command)

    monkeypatch.setattr(reference, "rng", SimpleNamespace(uniform=lambda *_: 0))
    monkeypatch.setattr(reference, "devices", {device["id"]: device})
    monkeypatch.setattr(reference, "handle_download", forbidden)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"commands": [{"type": "DOWNLOAD"}]})
        )
    ) as client:
        monkeypatch.setattr(reference, "client", client)
        await adapter.duplicate_post(
            "/devices/GW-TEST/command-response", {"deviceId": "GW-TEST", "status": "ACCEPTED"}
        )
    assert calls == []


@pytest.mark.parametrize("startup_fails", [False, True])
async def test_adapter_scopes_patch_to_its_lifespan(monkeypatch, startup_fails):
    original = reference.duplicate_post
    entered = []

    @asynccontextmanager
    async def lifecycle(app):
        entered.append(app)
        assert reference.duplicate_post is adapter.duplicate_post
        if startup_fails:
            raise RuntimeError("simulated startup failure")
        yield

    monkeypatch.setattr(reference, "lifespan", lifecycle)
    if startup_fails:
        with pytest.raises(RuntimeError, match="startup failure"):
            async with adapter.lifespan(adapter.app):
                pass
    else:
        async with adapter.lifespan(adapter.app):
            assert reference.duplicate_post is adapter.duplicate_post
    assert entered == [adapter.app]
    assert reference.duplicate_post is original


async def test_adapter_exposes_original_endpoints(monkeypatch):
    monkeypatch.setattr(reference, "devices", {})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=adapter.app), base_url="http://simulator"
    ) as client:
        assert (await client.get("/devices", params={"version": "1.0.0"})).json() == []
        assert (await client.get("/_admin/summary")).json()["busy"] == 0
        assert (await client.get("/openapi.json")).status_code == 200
        assert (await client.post("/devices/missing/connection-request")).status_code == 404
