"""Exercise the supplied simulator's real protocol against the PostgreSQL controller."""

import asyncio
import json
import random
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from controller.db import db_now
from controller.models import CampaignDevice, DeviceEvent
from controller.service import tick
from scripts import fw_simulator as supplied

pytestmark = pytest.mark.integration


class ImmediateDeviceDelays(random.Random):
    def uniform(self, minimum, maximum):
        # Exercise the supplied late-result branch without a real 60-second wait.
        return 0 if minimum == 60 else minimum


@pytest.fixture
async def supplied_fleet(client, monkeypatch):
    from controller.api import app

    device = {
        "id": "GW-A12B34C56D78",
        "version": "1.0.0",
        "profile": "healthy",
        "busy": False,
    }
    monkeypatch.setattr(supplied, "devices", {device["id"]: device})
    monkeypatch.setattr(supplied, "background_tasks", set())
    monkeypatch.setattr(supplied, "rng", ImmediateDeviceDelays(42))
    for name in ("DOWNLOAD_MIN_SEC", "DOWNLOAD_MAX_SEC", "REBOOT_MIN_SEC", "REBOOT_MAX_SEC"):
        monkeypatch.setattr(supplied, name, 0)
    monkeypatch.setattr(supplied, "P_DUPLICATE", 0)
    monkeypatch.setattr(supplied, "P_LATE", 0)
    harness = SimpleNamespace(device=device, calls=[], fail_ack=False, duplicate_release=None)

    async def callback(request):
        body = json.loads(request.content)
        record = {"path": request.url.path, "body": body}
        harness.calls.append(record)
        if harness.duplicate_release is not None and len(harness.calls) == 2:
            await harness.duplicate_release.wait()
        if harness.fail_ack and request.url.path.endswith("/command-response"):
            response = httpx.Response(503, json={"detail": "simulated ACK transport failure"})
        else:
            response = await client.post(request.url.path, json=body)
        record["status"] = response.status_code
        record["response"] = response.json()
        return response

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(callback)) as callbacks,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=supplied.app), base_url="http://supplied"
        ) as simulator,
    ):
        monkeypatch.setattr(supplied, "client", callbacks)
        monkeypatch.setattr(app.state, "http", simulator)
        harness.simulator = simulator
        yield harness
        tasks = list(supplied.background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def enroll(client, database):
    response = await client.post(
        "/campaigns",
        json={
            "targetVersion": "2.0.0",
            "imageUrl": "https://firmware.example/2.0.0.bin",
            "deviceFilter": {"currentVersion": "1.0.0"},
            "maxConcurrent": 1,
            "failureThresholdPercent": 100,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["totalDevices"] == 1
    return response.json()["id"]


async def schedule(database, seconds=0):
    async with database.begin() as session:
        return await tick(session, await db_now(session) + timedelta(seconds=seconds))


async def drain():
    async def finish():
        while supplied.background_tasks:
            await asyncio.gather(*list(supplied.background_tasks))
            await asyncio.sleep(0)

    await asyncio.wait_for(finish(), timeout=10)


async def attempt(database):
    async with database() as session:
        return await session.scalar(select(CampaignDevice))


async def authorizations(database):
    async with database() as session:
        return await session.scalar(
            select(func.count())
            .select_from(DeviceEvent)
            .where(DeviceEvent.reason == "COMMAND_AUTHORIZED")
        )


@pytest.mark.parametrize(
    ("profile", "expected_state", "expected_version", "expected_evidence"),
    [
        ("healthy", "SUCCEEDED", "2.0.0", "transfer_complete"),
        ("reject", "FAILED", "1.0.0", "ack"),
        ("fault", "FAILED", "1.0.0", "transfer_complete"),
        ("no_tc", "SUCCEEDED", "2.0.0", "boot_inferred"),
        ("revert", "FAILED", "1.0.0", "boot_inferred"),
        ("offline", "TIMED_OUT", "1.0.0", "deadline"),
    ],
)
async def test_supplied_profiles_use_actual_http_contract(
    client, database, supplied_fleet, profile, expected_state, expected_version, expected_evidence
):
    device = supplied_fleet.device
    device["profile"] = profile
    campaign_id = await enroll(client, database)
    jobs = await schedule(database)
    assert len(jobs) == 1
    response = await supplied_fleet.simulator.post(f"/devices/{device['id']}/connection-request")
    assert response.status_code == 202
    await drain()
    if profile == "offline":
        assert supplied_fleet.calls == []
        assert (await attempt(database)).state == "NOTIFIED"
        await schedule(database, seconds=1000)
    elif profile == "revert":
        assert (await attempt(database)).state == "AWAITING_RESULT"
        await schedule(database, seconds=1000)
    row = await attempt(database)
    assert row.state == expected_state
    assert row.evidence == expected_evidence
    assert device["version"] == expected_version
    assert device["busy"] is False
    assert await authorizations(database) == (0 if profile == "offline" else 1)
    assert all(call["status"] == 200 for call in supplied_fleet.calls)
    if profile in ("healthy", "fault"):
        transfer = next(
            call["body"]["transferComplete"]
            for call in supplied_fleet.calls
            if "transferComplete" in call["body"]
        )
        assert isinstance(transfer["startTime"], float)
        assert isinstance(transfer["completeTime"], float)
        assert transfer["commandKey"] == str(row.command_key)
        assert transfer["faultCode"] == (9010 if profile == "fault" else 0)
    status = (await client.get(f"/campaigns/{campaign_id}")).json()
    assert status["status"] == "COMPLETED"
    assert status["slotsHeld"] == 0
    assert status["counts"][expected_state] == 1


async def test_supplied_duplicate_callbacks_never_authorize_another_download(
    client, database, supplied_fleet, monkeypatch
):
    monkeypatch.setattr(supplied, "P_DUPLICATE", 1)
    await enroll(client, database)
    await schedule(database)
    await supplied.inform(supplied_fleet.device, "CONNECTION_REQUEST")
    await drain()
    assert (await attempt(database)).state == "SUCCEEDED"
    assert await authorizations(database) == 1
    completions = [call for call in supplied_fleet.calls if "transferComplete" in call["body"]]
    acknowledgments = [call for call in supplied_fleet.calls if "status" in call["body"]]
    assert len(completions) == len(acknowledgments) == 2
    assert completions[0]["body"] == completions[1]["body"]
    assert sum(len(call["response"].get("commands", [])) for call in supplied_fleet.calls) == 1


@pytest.mark.parametrize("profile", ["healthy", "fault"])
async def test_supplied_late_completion_follows_boot_without_regression(
    client, database, supplied_fleet, monkeypatch, profile
):
    supplied_fleet.device["profile"] = profile
    monkeypatch.setattr(supplied, "P_LATE", 1)
    await enroll(client, database)
    await schedule(database)
    await supplied.inform(supplied_fleet.device, "CONNECTION_REQUEST")
    await drain()
    events = [call["body"]["event"] for call in supplied_fleet.calls if "event" in call["body"]]
    assert events == ["CONNECTION_REQUEST", "BOOT", "TRANSFER_COMPLETE"]
    row = await attempt(database)
    assert row.state == ("SUCCEEDED" if profile == "healthy" else "FAILED")
    assert await authorizations(database) == 1


async def test_supplied_missing_ack_does_not_prevent_keyed_completion(
    client, database, supplied_fleet
):
    supplied_fleet.fail_ack = True
    await enroll(client, database)
    await schedule(database)
    await supplied.inform(supplied_fleet.device, "CONNECTION_REQUEST")
    await drain()
    assert (await attempt(database)).state == "SUCCEEDED"
    acknowledgments = [call for call in supplied_fleet.calls if "status" in call["body"]]
    assert len(acknowledgments) == 1
    assert acknowledgments[0]["status"] == 503
    assert await authorizations(database) == 1


async def test_supplied_discarded_duplicate_response_is_not_unsafely_redispatched(
    client, database, supplied_fleet, monkeypatch
):
    # duplicate_post deliberately ignores returned commands. If admission occurs
    # between original and duplicate informs, delivery becomes genuinely unknown.
    monkeypatch.setattr(supplied, "P_DUPLICATE", 1)
    supplied_fleet.duplicate_release = asyncio.Event()
    campaign_id = await enroll(client, database)
    await supplied.inform(supplied_fleet.device, "CONNECTION_REQUEST")
    assert supplied_fleet.calls[0]["response"] == {"commands": []}
    await schedule(database)
    supplied_fleet.duplicate_release.set()
    await drain()
    row = await attempt(database)
    assert row.state == "DOWNLOAD_ISSUED"
    key = row.command_key
    assert supplied_fleet.device["busy"] is False
    assert len(supplied_fleet.calls[1]["response"]["commands"]) == 1
    await supplied.inform(supplied_fleet.device, "PERIODIC")
    await drain()
    await schedule(database, seconds=1000)
    row = await attempt(database)
    assert row.state == "OUTCOME_UNKNOWN"
    assert row.command_key == key
    assert await authorizations(database) == 1
    status = (await client.get(f"/campaigns/{campaign_id}")).json()
    assert status["status"] == "HALTED"
    assert status["slotsHeld"] == 1


async def test_supplied_lifespan_creates_seeded_fleet_and_cleans_up(monkeypatch):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(supplied, "DEVICE_COUNT", 3)
    monkeypatch.setattr(supplied, "fleet_rng", random.Random(42))
    monkeypatch.setattr(supplied, "rng", random.Random(43))
    monkeypatch.setattr(supplied, "devices", {})
    monkeypatch.setattr(supplied, "background_tasks", set())
    monkeypatch.setattr(supplied, "client", None)
    monkeypatch.setattr(
        supplied.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"commands": []})
            ),
        ),
    )
    async with supplied.lifespan(supplied.app):
        assert len(supplied.devices) == 3
        assert all(key.startswith("GW-") and len(key) == 15 for key in supplied.devices)
        assert all(
            device["version"] == supplied.OLD_VERSION for device in supplied.devices.values()
        )
        assert supplied.client is not None
    assert supplied.client is None
    assert not supplied.background_tasks
