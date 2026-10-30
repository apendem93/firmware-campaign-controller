import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from controller.models import SLOTS, Campaign, CampaignDevice, DeviceEvent

pytestmark = pytest.mark.integration


def inform(device="device-1", event="CONNECTION_REQUEST", version="1.0.0", completion=None):
    body = {"deviceId": device, "firmwareVersion": version, "event": event}
    if completion is not None:
        body["transferComplete"] = completion
    return body


def completion(key, fault=0):
    now = datetime.now(UTC)
    return {
        "commandKey": key,
        "faultCode": fault,
        "startTime": (now - timedelta(seconds=2)).isoformat(),
        "completeTime": now.isoformat(),
    }


async def create(client, database, **overrides):
    body = {
        "targetVersion": "2.0.0",
        "imageUrl": "https://firmware.example/image.bin",
        "deviceFilter": {"currentVersion": "1.0.0"},
        "maxConcurrent": 2,
        "failureThresholdPercent": 100,
    }
    body.update(overrides)
    response = await client.post("/campaigns", json=body)
    assert response.status_code == 201, response.text
    async with database() as session:
        return str(await session.scalar(select(Campaign.id)))


async def reserve(database, device="device-1"):
    async with database.begin() as session:
        row = await session.scalar(select(CampaignDevice).where(CampaignDevice.device_id == device))
        row.state = "NOTIFIED"
        row.inform_deadline = datetime.now(UTC) + timedelta(minutes=2)


async def dispatch(client, database, device="device-1"):
    await reserve(database, device)
    response = await client.post(f"/devices/{device}/inform", json=inform(device))
    assert response.status_code == 200, response.text
    commands = response.json()["commands"]
    assert len(commands) == 1
    assert commands[0]["type"] == "DOWNLOAD"
    assert commands[0]["targetVersion"] == "2.0.0"
    return commands[0]["commandKey"]


async def state(database, device="device-1"):
    async with database() as session:
        return await session.scalar(
            select(CampaignDevice.state).where(CampaignDevice.device_id == device)
        )


async def tick(database, seconds=0):
    from controller.db import db_now
    from controller.service import tick as scheduler_tick

    async with database.begin() as session:
        now = await db_now(session)
        return await scheduler_tick(session, now + timedelta(seconds=seconds))


async def test_enrollment_snapshots_inventory_and_rejects_overlap(client, database):
    campaign_id = await create(client, database)
    async with database() as session:
        campaign = await session.get(Campaign, uuid.UUID(campaign_id))
        assert campaign.total == 6
        assert await session.scalar(select(func.count()).select_from(CampaignDevice)) == 6
    response = await client.post(
        "/campaigns",
        json={
            "targetVersion": "3.0.0",
            "imageUrl": "https://firmware.example/3.bin",
            "deviceFilter": {"currentVersion": "1.0.0"},
            "maxConcurrent": 2,
            "failureThresholdPercent": 50,
        },
    )
    assert response.status_code == 409
    assert (await client.get(f"/campaigns/{campaign_id}")).status_code == 200


async def test_empty_fleet_completes(client, database):
    client.fleet.clear()
    campaign_id = await create(client, database)
    async with database() as session:
        campaign = await session.get(Campaign, uuid.UUID(campaign_id))
        assert campaign.status == "COMPLETED"
        assert campaign.total == 0


async def test_first_completion_after_boot_is_recorded_as_corroboration(client, database):
    campaign_id = await create(client, database)
    key = await dispatch(client, database)
    await client.post("/devices/device-1/inform", json=inform(event="BOOT", version="2.0.0"))
    payload = inform(event="TRANSFER_COMPLETE", version="2.0.0", completion=completion(key))
    for _ in range(2):
        assert (await client.post("/devices/device-1/inform", json=payload)).status_code == 200
    assert await state(database) == "SUCCEEDED"
    async with database() as session:
        rows = (
            await session.scalars(
                select(DeviceEvent).where(
                    DeviceEvent.campaign_id == uuid.UUID(campaign_id),
                    DeviceEvent.kind == "completion_observation",
                )
            )
        ).all()
        assert len(rows) == 1 and rows[0].reason == "LATE_CORROBORATION"
    diagnostics = (
        await client.get(f"/campaigns/{campaign_id}/devices", params={"state": "SUCCEEDED"})
    ).json()["items"][0]
    assert diagnostics["faultCode"] == 0
    assert diagnostics["evidence"] == "boot_inferred"


async def test_event_history_is_paginated_and_filterable(client, database):
    campaign_id = await create(client, database)
    key = await dispatch(client, database)
    collected = []
    after = 0
    while True:
        response = await client.get(
            f"/campaigns/{campaign_id}/events",
            params={"deviceId": "device-1", "limit": 1, "after": after},
        )
        assert response.status_code == 200
        page = response.json()
        collected.extend(page["items"])
        if page["nextCursor"] is None:
            break
        after = page["nextCursor"]
    assert len(collected) >= 2
    assert len({event["id"] for event in collected}) == len(collected)
    assert all(event["deviceId"] == "device-1" for event in collected)
    assert any(
        event["commandKey"] == key and event["reason"] == "COMMAND_AUTHORIZED"
        for event in collected
    )
    filtered = (
        await client.get(f"/campaigns/{campaign_id}/events", params={"commandKey": key})
    ).json()["items"]
    assert filtered and all(event["commandKey"] == key for event in filtered)
    assert (await client.get(f"/campaigns/{uuid.uuid4()}/events")).status_code == 404
    assert (
        await client.get(f"/campaigns/{campaign_id}/events", params={"limit": 501})
    ).status_code == 422


async def test_explicit_fault_wins_but_conflicting_target_firmware_is_audited(client, database):
    campaign_id = await create(client, database)
    key = await dispatch(client, database)
    response = await client.post(
        "/devices/device-1/inform",
        json=inform(event="TRANSFER_COMPLETE", version="2.0.0", completion=completion(key, 9010)),
    )
    assert response.status_code == 200
    assert await state(database) == "FAILED"
    events = (
        await client.get(f"/campaigns/{campaign_id}/events", params={"commandKey": key})
    ).json()["items"]
    assert any(event["reason"] == "FAULT_WITH_TARGET_VERSION" for event in events)


async def test_late_old_boot_resolves_unknown_outcome(client, database):
    await create(client, database)
    await dispatch(client, database)
    await tick(database, seconds=1000)
    assert await state(database) == "OUTCOME_UNKNOWN"
    response = await client.post("/devices/device-1/inform", json=inform(event="BOOT"))
    assert response.status_code == 200
    assert await state(database) == "FAILED"


async def test_naive_completion_timestamps_assume_utc(client, database):
    await create(client, database)
    key = await dispatch(client, database)
    payload = completion(key)
    payload["startTime"] = "2026-01-01T00:00:00"
    payload["completeTime"] = "2026-01-01T00:00:01"
    response = await client.post(
        "/devices/device-1/inform",
        json=inform(event="TRANSFER_COMPLETE", version="2.0.0", completion=payload),
    )
    assert response.status_code == 200
    assert await state(database) == "SUCCEEDED"


async def test_inform_after_admission_deadline_cannot_dispatch(client, database):
    await create(client, database)
    await reserve(database)
    async with database.begin() as session:
        row = await session.scalar(
            select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
        )
        row.inform_deadline = datetime.now(UTC) - timedelta(seconds=1)
    response = await client.post("/devices/device-1/inform", json=inform())
    assert response.json() == {"commands": []}
    assert await state(database) == "TIMED_OUT"


@pytest.mark.parametrize("expires_while_waiting", [False, True])
async def test_queued_inform_uses_time_after_campaign_lock(
    client, database, monkeypatch, expires_while_waiting
):
    from controller import service

    campaign_id = await create(client, database)
    await reserve(database)
    waiting = asyncio.Event()
    original_lock = service.lock_campaign

    async def observe_lock_wait(session, identifier):
        waiting.set()
        return await original_lock(session, identifier)

    monkeypatch.setattr(service, "lock_campaign", observe_lock_wait)
    request = None
    try:
        async with database.begin() as session:
            campaign = await session.scalar(
                select(Campaign).where(Campaign.id == uuid.UUID(campaign_id)).with_for_update()
            )
            request = asyncio.create_task(client.post("/devices/device-1/inform", json=inform()))
            await asyncio.wait_for(waiting.wait(), timeout=10)
            assert not request.done()
            released_at = await session.scalar(select(func.clock_timestamp()))
            if expires_while_waiting:
                row = await session.scalar(
                    select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
                )
                # Put expiry inside the lock wait without relying on a timed sleep.
                row.inform_deadline = released_at
        response = await asyncio.wait_for(request, timeout=10)
    finally:
        if request is not None and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    assert response.status_code == 200
    async with database() as session:
        row = await session.scalar(
            select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
        )
        if expires_while_waiting:
            assert response.json() == {"commands": []}
            assert row.state == "TIMED_OUT"
            assert row.command_key is None
        else:
            assert len(response.json()["commands"]) == 1
            assert row.dispatched_at >= released_at
            assert row.ack_deadline - row.dispatched_at == timedelta(
                seconds=campaign.tuning["ack_timeout"]
            )
            assert row.result_deadline - row.dispatched_at == timedelta(
                seconds=campaign.tuning["result_timeout"]
            )


async def test_admission_reserves_only_immediately_available_nudge_capacity(client, database):
    campaign_id = await create(client, database, maxConcurrent=5)
    async with database.begin() as session:
        campaign = await session.get(Campaign, uuid.UUID(campaign_id))
        campaign.tuning = {**campaign.tuning, "max_http_concurrency": 1}
    jobs = await tick(database)
    assert len(jobs) == 1
    status = (await client.get(f"/campaigns/{campaign_id}")).json()
    assert status["counts"]["NOTIFIED"] == 1
    assert status["counts"]["PENDING"] == 5


async def test_frozen_migration_round_trip_matches_models(database):
    import os
    import sys

    from sqlalchemy import text

    from controller.db import engine
    from controller.models import Base

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    for arguments in [("upgrade", "head"), ("check",), ("downgrade", "base"), ("upgrade", "head")]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            *arguments,
            env=dict(os.environ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        assert process.returncode == 0, output.decode()
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE alembic_version"))


async def test_command_is_persisted_before_response_and_never_redispatched(client, database):
    await create(client, database)
    key = await dispatch(client, database)
    async with database() as session:
        row = await session.scalar(
            select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
        )
        assert str(row.command_key) == key
        assert row.dispatched_at is not None
        assert row.result_deadline > row.dispatched_at
    for _ in range(3):
        response = await client.post("/devices/device-1/inform", json=inform())
        assert response.json() == {"commands": []}


async def test_concurrent_informs_authorize_exactly_one_download(client, database):
    await create(client, database)
    await reserve(database)
    responses = await asyncio.gather(
        *[client.post("/devices/device-1/inform", json=inform()) for _ in range(8)]
    )
    assert all(response.status_code == 200 for response in responses)
    commands = [command for response in responses for command in response.json()["commands"]]
    assert len(commands) == 1


async def test_completion_before_ack_and_duplicate_callbacks_do_not_regress(client, database):
    await create(client, database)
    key = await dispatch(client, database)
    payload = inform(event="TRANSFER_COMPLETE", version="2.0.0", completion=completion(key))
    for _ in range(3):
        assert (await client.post("/devices/device-1/inform", json=payload)).status_code == 200
    assert (
        await client.post(
            "/devices/device-1/command-response",
            json={
                "commandKey": key,
                "status": "ACCEPTED",
            },
        )
    ).status_code == 200
    assert await state(database) == "SUCCEEDED"
    async with database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CampaignDevice)
                .where(CampaignDevice.state == "SUCCEEDED")
            )
            == 1
        )


@pytest.mark.parametrize("fault", [9001, 9010])
async def test_nonzero_completion_fault_is_failure(client, database, fault):
    await create(client, database)
    key = await dispatch(client, database)
    response = await client.post(
        "/devices/device-1/inform",
        json=inform(
            event="TRANSFER_COMPLETE",
            completion=completion(key, fault),
        ),
    )
    assert response.status_code == 200
    assert await state(database) == "FAILED"


async def test_rejection_is_terminal_and_late_acceptance_cannot_regress(client, database):
    await create(client, database)
    key = await dispatch(client, database)
    for acknowledgment in ["REJECTED", "REJECTED", "ACCEPTED"]:
        response = await client.post(
            "/devices/device-1/command-response",
            json={
                "commandKey": key,
                "status": acknowledgment,
                "reason": "unsupported image",
            },
        )
        assert response.status_code == 200
    assert await state(database) == "FAILED"


async def test_target_boot_without_completion_succeeds_and_old_boot_does_not_regress(
    client, database
):
    await create(client, database)
    await dispatch(client, database)
    response = await client.post(
        "/devices/device-1/inform", json=inform(event="BOOT", version="2.0.0")
    )
    assert response.status_code == 200
    assert await state(database) == "SUCCEEDED"
    await client.post("/devices/device-1/inform", json=inform(event="BOOT"))
    assert await state(database) == "SUCCEEDED"


async def test_periodic_target_version_does_not_replace_boot_or_completion_evidence(
    client, database
):
    await create(client, database)
    await dispatch(client, database)
    await client.post("/devices/device-1/inform", json=inform(event="PERIODIC", version="2.0.0"))
    assert await state(database) not in ("SUCCEEDED", "FAILED", "TIMED_OUT")


async def test_old_boot_waits_for_completion_deadline(client, database):
    await create(client, database)
    await dispatch(client, database)
    await client.post("/devices/device-1/inform", json=inform(event="BOOT"))
    assert await state(database) in ("DOWNLOAD_ISSUED", "AWAITING_RESULT")


async def test_pause_blocks_dispatch_but_allows_active_completion(client, database):
    campaign_id = await create(client, database)
    key = await dispatch(client, database)
    await reserve(database, "device-2")
    for _ in range(2):
        assert (await client.post(f"/campaigns/{campaign_id}/pause")).status_code == 200
    response = await client.post("/devices/device-2/inform", json=inform("device-2"))
    assert response.json() == {"commands": []}
    await client.post(
        "/devices/device-1/inform",
        json=inform(
            event="TRANSFER_COMPLETE",
            version="2.0.0",
            completion=completion(key),
        ),
    )
    assert await state(database) == "SUCCEEDED"
    assert (await client.post(f"/campaigns/{campaign_id}/resume")).status_code == 200


async def test_foreign_device_cannot_acknowledge_another_command(client, database):
    await create(client, database)
    key = await dispatch(client, database)
    response = await client.post(
        "/devices/device-2/command-response",
        json={
            "commandKey": key,
            "status": "REJECTED",
        },
    )
    assert response.status_code == 409
    assert await state(database) == "DOWNLOAD_ISSUED"


async def test_unknown_completion_key_cannot_dispatch_or_mutate(client, database):
    await create(client, database)
    await reserve(database)
    response = await client.post(
        "/devices/device-1/inform",
        json=inform(
            event="TRANSFER_COMPLETE",
            version="2.0.0",
            completion=completion(str(uuid.uuid4())),
        ),
    )
    assert response.status_code == 200
    assert response.json() == {"commands": []}
    assert await state(database) == "NOTIFIED"


@pytest.mark.parametrize(
    "change",
    [
        {"deviceId": "different-device"},
        {"event": "UNKNOWN"},
        {"event": "TRANSFER_COMPLETE"},
    ],
)
async def test_invalid_informs_are_rejected(client, change):
    payload = inform()
    payload.update(change)
    response = await client.post("/devices/device-1/inform", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "change",
    [
        {"maxConcurrent": 0},
        {"failureThresholdPercent": -1},
        {"failureThresholdPercent": 101},
        {"targetVersion": "1.0.0"},
        {"imageUrl": "file:///etc/passwd"},
    ],
)
async def test_invalid_campaign_settings_are_rejected(client, change):
    payload = {
        "targetVersion": "2.0.0",
        "imageUrl": "https://firmware.example/image.bin",
        "deviceFilter": {"currentVersion": "1.0.0"},
        "maxConcurrent": 2,
        "failureThresholdPercent": 50,
    }
    payload.update(change)
    response = await client.post("/campaigns", json=payload)
    assert response.status_code == 422


async def test_metrics_use_route_templates_and_capture_status_codes(client, database):
    await create(client, database)
    await client.post("/devices/device-1/inform", json=inform())
    await client.get(f"/campaigns/{uuid.uuid4()}")
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "/devices/{device_id}/inform" in response.text
    assert "device-1" not in response.text


async def test_transition_audit_contains_command_identity(client, database):
    await create(client, database)
    key = await dispatch(client, database)
    await client.post(
        "/devices/device-1/command-response", json={"commandKey": key, "status": "ACCEPTED"}
    )
    async with database() as session:
        events = list(
            (
                await session.scalars(
                    select(DeviceEvent).where(DeviceEvent.device_id == "device-1")
                )
            ).all()
        )
        assert events
        assert any(str(event.command_key) == key for event in events)


async def test_parallel_scheduler_ticks_never_exceed_concurrency(client, database):
    await create(client, database, maxConcurrent=2)
    await asyncio.gather(*(tick(database) for _ in range(6)))
    async with database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CampaignDevice)
                .where(CampaignDevice.state.in_(SLOTS))
            )
            == 2
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CampaignDevice)
                .where(CampaignDevice.state == "PENDING")
            )
            == 4
        )


async def test_expired_nudge_lease_is_reclaimed_with_bounded_retries(client, database):
    await create(client, database, maxConcurrent=1)
    first = await tick(database)
    assert len(first) == 1
    assert await tick(database) == []
    second = await tick(database, seconds=16)
    assert len(second) == 1
    assert first[0]["attempt_id"] == second[0]["attempt_id"]
    assert first[0]["token"] != second[0]["token"]
    third = await tick(database, seconds=32)
    assert len(third) == 1
    assert await tick(database, seconds=48) == []
    async with database() as session:
        row = await session.get(CampaignDevice, first[0]["attempt_id"])
        assert row.nudge_attempts == 3
        assert row.command_key is None


async def test_no_session_deadline_times_out_and_releases_slot(client, database):
    await create(client, database, maxConcurrent=1)
    jobs = await tick(database)
    await tick(database, seconds=91)
    async with database() as session:
        row = await session.get(CampaignDevice, jobs[0]["attempt_id"])
        assert row.state == "TIMED_OUT"
        assert row.command_key is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CampaignDevice)
                .where(CampaignDevice.state.in_(SLOTS))
            )
            == 1
        )


async def test_missing_ack_waits_without_resend_and_accepts_late_ack(client, database):
    await create(client, database)
    key = await dispatch(client, database)
    await tick(database, seconds=31)
    assert await state(database) == "AWAITING_RESULT"
    async with database() as session:
        row = await session.scalar(
            select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
        )
        assert row.ack_status == "MISSING"
        assert str(row.command_key) == key
    response = await client.post("/devices/device-1/inform", json=inform())
    assert response.json() == {"commands": []}
    await client.post(
        "/devices/device-1/command-response", json={"commandKey": key, "status": "ACCEPTED"}
    )
    async with database() as session:
        row = await session.scalar(
            select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
        )
        assert row.ack_status == "ACCEPTED"
        assert row.state == "AWAITING_RESULT"


async def test_unknown_outcome_retains_slot_halts_and_reconciles_late_success(client, database):
    campaign_id = await create(client, database, maxConcurrent=1)
    key = await dispatch(client, database)
    assert await tick(database, seconds=901) == []
    assert await state(database) == "OUTCOME_UNKNOWN"
    progress = (await client.get(f"/campaigns/{campaign_id}")).json()
    assert progress["status"] == "HALTED"
    assert progress["slotsHeld"] == 1
    assert progress["counts"]["OUTCOME_UNKNOWN"] == 1
    assert (await client.post(f"/campaigns/{campaign_id}/resume")).status_code == 409
    await client.post(
        "/devices/device-1/inform",
        json=inform(
            event="TRANSFER_COMPLETE",
            version="2.0.0",
            completion=completion(key),
        ),
    )
    assert await state(database) == "SUCCEEDED"
    progress = (await client.get(f"/campaigns/{campaign_id}")).json()
    assert progress["status"] == "HALTED"
    assert progress["slotsHeld"] == 0
    assert (await client.post(f"/campaigns/{campaign_id}/resume")).status_code == 200


async def test_old_boot_becomes_inferred_failure_only_at_result_deadline(client, database):
    await create(client, database)
    await dispatch(client, database)
    await client.post("/devices/device-1/inform", json=inform(event="BOOT"))
    await tick(database, seconds=31)
    assert await state(database) == "AWAITING_RESULT"
    await tick(database, seconds=901)
    assert await state(database) == "FAILED"
    async with database() as session:
        row = await session.scalar(
            select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
        )
        assert row.outcome_reason == "BOOT_OLD_VERSION"
        assert row.evidence == "boot_inferred"


async def test_zero_fault_wrong_version_requires_verification(client, database):
    await create(client, database)
    key = await dispatch(client, database)
    payload = inform(event="TRANSFER_COMPLETE", completion=completion(key))
    await client.post("/devices/device-1/inform", json=payload)
    assert await state(database) == "AWAITING_RESULT"
    async with database() as session:
        row = await session.scalar(
            select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
        )
        original_deadline = row.verification_deadline
    await client.post("/devices/device-1/inform", json=payload)
    async with database() as session:
        row = await session.scalar(
            select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
        )
        assert row.verification_deadline == original_deadline
    await tick(database, seconds=31)
    assert await state(database) == "FAILED"


async def test_failure_gate_minimum_strict_threshold_and_explicit_resume_epoch(client, database):
    campaign_id = await create(client, database, maxConcurrent=3, failureThresholdPercent=50)
    async with database.begin() as session:
        campaign = await session.get(Campaign, uuid.UUID(campaign_id))
        campaign.gate_minimum = 2
    key1 = await dispatch(client, database, "device-1")
    await client.post(
        "/devices/device-1/command-response", json={"commandKey": key1, "status": "REJECTED"}
    )
    progress = (await client.get(f"/campaigns/{campaign_id}")).json()
    assert progress["status"] == "RUNNING"  # 100%, but below the sample minimum.
    key2 = await dispatch(client, database, "device-2")
    await client.post(
        "/devices/device-2/inform",
        json=inform(
            "device-2",
            "TRANSFER_COMPLETE",
            "2.0.0",
            completion(key2),
        ),
    )
    progress = (await client.get(f"/campaigns/{campaign_id}")).json()
    assert progress["failureRate"] == 50
    assert progress["status"] == "RUNNING"  # Equality must not halt.
    key3 = await dispatch(client, database, "device-3")
    await reserve(database, "device-4")
    await client.post(
        "/devices/device-3/command-response", json={"commandKey": key3, "status": "REJECTED"}
    )
    progress = (await client.get(f"/campaigns/{campaign_id}")).json()
    assert progress["status"] == "HALTED"
    assert progress["failureRate"] == pytest.approx(200 / 3)
    assert await state(database, "device-4") == "PENDING"
    response = await client.post("/devices/device-4/inform", json=inform("device-4"))
    assert response.json() == {"commands": []}
    assert (await client.post(f"/campaigns/{campaign_id}/resume")).status_code == 200
    progress = (await client.get(f"/campaigns/{campaign_id}")).json()
    assert progress["failureGate"]["epoch"] == 1
    assert progress["failureGate"]["minimumSample"] == 3
    assert progress["failureGate"]["sampleSize"] == 0
    assert progress["failureRate"] == pytest.approx(200 / 3)


async def test_database_reconnect_preserves_command_key_and_deadline(client, database):
    from controller.db import engine

    await create(client, database)
    key = await dispatch(client, database)
    async with database() as session:
        row = await session.scalar(
            select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
        )
        original_deadline = row.result_deadline
    await engine.dispose()
    await tick(database)
    response = await client.post("/devices/device-1/inform", json=inform())
    assert response.json() == {"commands": []}
    async with database() as session:
        row = await session.scalar(
            select(CampaignDevice).where(CampaignDevice.device_id == "device-1")
        )
        assert str(row.command_key) == key
        assert row.result_deadline == original_deadline


async def test_callback_before_nudge_response_does_not_reset_dispatched_state(client, database):
    from controller.worker import send_nudge

    await create(client, database, maxConcurrent=1)
    job = (await tick(database))[0]
    commands = []

    async def respond(request):
        response = await client.post(
            f"/devices/{job['device_id']}/inform", json=inform(job["device_id"])
        )
        commands.extend(response.json()["commands"])
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as outbound:
        await send_nudge(outbound, job)
    assert len(commands) == 1
    async with database() as session:
        row = await session.get(CampaignDevice, job["attempt_id"])
        assert row.state == "DOWNLOAD_ISSUED"
        assert str(row.command_key) == commands[0]["commandKey"]
        assert row.lease_token is None


async def test_stale_nudge_lease_cannot_send_request(client, database):
    from controller.worker import send_nudge

    await create(client, database, maxConcurrent=1)
    old_job = (await tick(database))[0]
    new_job = (await tick(database, seconds=16))[0]
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as outbound:
        await send_nudge(outbound, old_job)
    assert requests == []
    async with database() as session:
        row = await session.get(CampaignDevice, old_job["attempt_id"])
        assert row.lease_token == new_job["token"]


async def test_simulator_failure_does_not_create_partial_campaign(client, database):
    from controller.api import app

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as outbound:
        original = app.state.http
        app.state.http = outbound
        try:
            response = await client.post(
                "/campaigns",
                json={
                    "targetVersion": "2.0.0",
                    "imageUrl": "https://firmware.example/image.bin",
                    "deviceFilter": {"currentVersion": "1.0.0"},
                    "maxConcurrent": 2,
                    "failureThresholdPercent": 50,
                },
            )
        finally:
            app.state.http = original
    assert response.status_code == 502
    assert len(requests) == 3
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Campaign)) == 0
        assert await session.scalar(select(func.count()).select_from(CampaignDevice)) == 0
