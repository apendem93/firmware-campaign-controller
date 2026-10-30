import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from controller.config import settings
from controller.db import db_now, engine, sessions
from controller.models import (
    ACTIVE,
    SLOTS,
    STATES,
    TERMINAL,
    Campaign,
    CampaignDevice,
    Device,
    DeviceEvent,
)
from controller.observability import CALLBACKS, INCOMING, LATENCY, configure_logging, outbound
from controller.schemas import CommandResponse, CreateCampaign, FleetDevice, Inform
from controller.service import (
    active_attempt,
    audit,
    counts,
    keyed_attempt,
    lock_campaign,
    park_unsent,
    process_ack,
    process_inform,
    progress,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    configure_logging()
    settings().tuning()
    async with httpx.AsyncClient(timeout=settings().tuning().http_timeout) as client:
        app.state.http = client
        yield
    await engine.dispose()


app = FastAPI(title="Firmware Campaign Controller", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def instrument(request: Request, call_next):
    started = time.monotonic()
    request_id = str(uuid.uuid4())
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        elapsed = time.monotonic() - started
        INCOMING.labels(request.method, route, str(status)).inc()
        LATENCY.labels(request.method, route).observe(elapsed)
        log.info(
            "http request",
            extra={
                "fields": {
                    "request_id": request_id,
                    "method": request.method,
                    "endpoint": route,
                    "status": status,
                    "duration_seconds": elapsed,
                }
            },
        )


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    log.error("database request failed", extra={"fields": {"error_type": type(exc).__name__}})
    return JSONResponse(
        status_code=503, content={"detail": "Database unavailable; retry the request"}
    )


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.get("/readyz")
async def ready():
    async with sessions() as session:
        await session.execute(text("SELECT version_num FROM alembic_version"))
        await session.execute(select(Campaign.id).limit(1))
    return {"status": "ready"}


@app.post("/campaigns", status_code=201)
async def create_campaign(body: CreateCampaign, request: Request):
    tuning = settings().tuning()
    async with sessions() as session:
        if await session.scalar(select(Campaign.id).where(Campaign.status.in_(ACTIVE)).limit(1)):
            raise HTTPException(409, "An active campaign already exists")
    response = None
    for attempt in range(3):
        try:
            response = await outbound(
                request.app.state.http,
                "GET",
                settings().simulator_url.rstrip("/") + "/devices",
                "fleet_discovery",
                params={"version": body.deviceFilter.currentVersion},
                timeout=tuning.http_timeout,
            )
            if response.status_code < 500 and response.status_code != 429:
                break
        except httpx.TimeoutException:
            if attempt == 2:
                raise HTTPException(504, "Simulator discovery timed out") from None
        except httpx.HTTPError:
            if attempt == 2:
                raise HTTPException(502, "Simulator discovery unavailable") from None
        if attempt < 2:
            await asyncio.sleep(0.1 * 2**attempt)
    if response is None or not response.is_success:
        raise HTTPException(502, "Simulator discovery failed")
    try:
        raw = response.json()
        if not isinstance(raw, list) or len(raw) > 10000:
            raise ValueError("Expected a fleet list of at most 10000 devices")
        fleet = {}
        for item in raw:
            device = FleetDevice.model_validate(item)
            if device.firmwareVersion != body.deviceFilter.currentVersion:
                raise ValueError("Simulator returned a device outside the requested filter")
            if device.deviceId in fleet and fleet[device.deviceId] != device.firmwareVersion:
                raise ValueError("Conflicting inventory")
            fleet[device.deviceId] = device.firmwareVersion
    except (ValueError, ValidationError):
        raise HTTPException(502, "Invalid simulator inventory") from None
    campaign = Campaign(
        target_version=body.targetVersion,
        image_url=str(body.imageUrl),
        current_version=body.deviceFilter.currentVersion,
        max_concurrent=body.maxConcurrent,
        failure_threshold=body.failureThresholdPercent,
        total=len(fleet),
        tuning=tuning.model_dump(),
        gate_minimum=min(tuning.minimum_sample, len(fleet)),
    )
    try:
        async with sessions.begin() as session:
            session.add(campaign)
            await session.flush()
            entries = list(fleet.items())
            for offset in range(0, len(entries), 1000):
                batch = entries[offset : offset + 1000]
                await session.execute(
                    insert(Device)
                    .values([{"device_id": key, "firmware": version} for key, version in batch])
                    .on_conflict_do_nothing(index_elements=["device_id"])
                )
                await session.execute(
                    insert(CampaignDevice),
                    [
                        {
                            "id": uuid.uuid4(),
                            "campaign_id": campaign.id,
                            "device_id": key,
                            "initial_version": version,
                            "state": "PENDING",
                            "nudge_attempts": 0,
                        }
                        for key, version in batch
                    ],
                )
            if not fleet:
                campaign.status = "COMPLETED"
                campaign.completed_at = await db_now(session)
            audit(session, campaign, None, "created", payload={"eligible": len(fleet)})
            result = await progress(session, campaign)
    except IntegrityError:
        raise HTTPException(409, "An active campaign already exists") from None
    return result


@app.get("/campaigns/{campaign_id}")
async def campaign_status(campaign_id: uuid.UUID):
    async with sessions.begin() as session:
        campaign = await lock_campaign(session, campaign_id)
        return await progress(session, campaign)


@app.get("/campaigns/{campaign_id}/devices")
async def campaign_devices(
    campaign_id: uuid.UUID,
    state: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    cursor: str | None = None,
):
    if state is not None and state not in STATES:
        raise HTTPException(422, "Invalid device state")
    async with sessions() as session:
        if await session.get(Campaign, campaign_id) is None:
            raise HTTPException(404, "Unknown campaign")
        query = select(CampaignDevice).where(CampaignDevice.campaign_id == campaign_id)
        if state:
            query = query.where(CampaignDevice.state == state)
        if cursor:
            query = query.where(CampaignDevice.device_id > cursor)
        rows = (
            await session.scalars(query.order_by(CampaignDevice.device_id).limit(limit + 1))
        ).all()
        return {
            "items": [
                {
                    "deviceId": row.device_id,
                    "state": row.state,
                    "commandKey": str(row.command_key) if row.command_key else None,
                    "reason": row.outcome_reason,
                    "evidence": row.evidence,
                    "nudgeAttempts": row.nudge_attempts,
                    "ackStatus": row.ack_status,
                    "faultCode": row.completion_fault,
                    "lastBootVersion": row.boot_version,
                    "dispatchedAt": row.dispatched_at,
                    "resultDeadline": row.result_deadline,
                    "finishedAt": row.finished_at,
                }
                for row in rows[:limit]
            ],
            "nextCursor": rows[limit - 1].device_id if len(rows) > limit else None,
        }


@app.get("/campaigns/{campaign_id}/events")
async def campaign_events(
    campaign_id: uuid.UUID,
    deviceId: str | None = Query(None, max_length=200),
    commandKey: str | None = Query(None, max_length=200),
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    async with sessions() as session:
        if await session.get(Campaign, campaign_id) is None:
            raise HTTPException(404, "Unknown campaign")
        query = select(DeviceEvent).where(
            DeviceEvent.campaign_id == campaign_id, DeviceEvent.id > after
        )
        if deviceId is not None:
            query = query.where(DeviceEvent.device_id == deviceId)
        if commandKey is not None:
            query = query.where(DeviceEvent.command_key == commandKey)
        rows = (await session.scalars(query.order_by(DeviceEvent.id).limit(limit + 1))).all()
        return {
            "items": [
                {
                    "id": row.id,
                    "deviceId": row.device_id,
                    "commandKey": row.command_key,
                    "kind": row.kind,
                    "fromState": row.previous_state,
                    "toState": row.new_state,
                    "reason": row.reason,
                    "payload": row.payload,
                    "receivedAt": row.received_at,
                }
                for row in rows[:limit]
            ],
            "nextCursor": rows[limit - 1].id if len(rows) > limit else None,
        }


@app.post("/campaigns/{campaign_id}/pause")
async def pause(campaign_id: uuid.UUID):
    async with sessions.begin() as session:
        campaign = await lock_campaign(session, campaign_id)
        if campaign.status == "COMPLETED":
            raise HTTPException(409, "Campaign is completed")
        if campaign.status == "RUNNING":
            campaign.status = "PAUSED"
            await park_unsent(session, campaign, await db_now(session))
            audit(session, campaign, None, "pause")
        return await progress(session, campaign)


@app.post("/campaigns/{campaign_id}/resume")
async def resume(campaign_id: uuid.UUID):
    async with sessions.begin() as session:
        campaign = await lock_campaign(session, campaign_id)
        if campaign.status == "COMPLETED":
            raise HTTPException(409, "Campaign is completed")
        values = await counts(session, campaign.id)
        if values["OUTCOME_UNKNOWN"]:
            raise HTTPException(
                409, "Outstanding unknown command outcomes must resolve before resume"
            )
        reset = campaign.status == "HALTED"
        if reset:
            campaign.gate_epoch += 1
            remaining = (
                campaign.total - values["SUCCEEDED"] - values["FAILED"] - values["TIMED_OUT"]
            )
            campaign.gate_minimum = min(campaign.tuning["minimum_sample"], remaining)
        if campaign.status != "RUNNING":
            campaign.status = "RUNNING"
            campaign.halt_reason = None
            audit(session, campaign, None, "resume", payload={"gateReset": reset})
        result = await progress(session, campaign)
        result["gateReset"] = reset
        return result


@app.post("/devices/{device_id}/inform")
async def inform(device_id: str, body: Inform):
    if device_id != body.deviceId:
        raise HTTPException(422, "Path and body deviceId must match")
    async with sessions.begin() as session:
        if body.transferComplete:
            campaign, attempt = await keyed_attempt(
                session, device_id, body.transferComplete.commandKey
            )
            if attempt is None:
                CALLBACKS.labels("unknown_key").inc()
                audit(
                    session,
                    None,
                    None,
                    "unknown_completion",
                    payload=body.model_dump(mode="json"),
                    command_key=body.transferComplete.commandKey,
                )
                return {"commands": []}
        else:
            campaign, attempt = await active_attempt(session, device_id)
        device = await session.get(Device, device_id)
        # Campaign-lock waits must not shorten new deadlines or admit expired sessions.
        now = await db_now(session)
        if device:
            device.firmware = body.firmwareVersion
            device.last_event = body.event
            device.observed_at = now
        commands = await process_inform(session, campaign, attempt, body, now) if attempt else []
    return {"commands": commands}


@app.post("/devices/{device_id}/command-response")
async def command_response(device_id: str, body: CommandResponse):
    async with sessions.begin() as session:
        campaign, attempt = await keyed_attempt(session, device_id, body.commandKey)
        if attempt is None:
            raise HTTPException(404, "Unknown commandKey")
        await process_ack(session, campaign, attempt, body, await db_now(session))
    return {"status": "recorded"}


@app.get("/metrics")
async def metrics():
    registry = CollectorRegistry()
    devices = Gauge(
        "controller_campaign_devices", "Devices in active campaign", ["state"], registry=registry
    )
    slots = Gauge(
        "controller_campaign_slots_held", "Slots held by active campaign", registry=registry
    )
    outcomes = Gauge(
        "controller_recorded_outcomes",
        "Durable outcomes across campaigns",
        ["state", "evidence"],
        registry=registry,
    )
    timeouts = Gauge(
        "controller_recorded_timeout_events",
        "Durable timeout transitions",
        ["reason"],
        registry=registry,
    )
    retries = Gauge(
        "controller_recorded_nudge_retries",
        "Recorded nudge responses after the first response per attempt",
        registry=registry,
    )
    async with sessions() as session:
        rows = await session.execute(
            select(CampaignDevice.state, func.count())
            .join(Campaign)
            .where(Campaign.status.in_(ACTIVE))
            .group_by(CampaignDevice.state)
        )
        values = dict(rows.all())
        result = await session.execute(
            select(CampaignDevice.state, CampaignDevice.evidence, func.count())
            .where(CampaignDevice.state.in_(TERMINAL))
            .group_by(CampaignDevice.state, CampaignDevice.evidence)
        )
        for state, evidence, count in result:
            outcomes.labels(state, evidence or "none").set(count)
        result = await session.execute(
            select(DeviceEvent.reason, func.count())
            .where(
                DeviceEvent.kind == "transition",
                DeviceEvent.reason.in_(
                    ("NO_SESSION", "ACK_DEADLINE", "RESULT_DEADLINE", "VERSION_MISMATCH")
                ),
            )
            .group_by(DeviceEvent.reason)
        )
        for reason, count in result:
            timeouts.labels(reason).set(count)
        nudge_counts = (
            select(func.greatest(func.count() - 1, 0).label("retries"))
            .where(DeviceEvent.kind == "nudge_response")
            .group_by(DeviceEvent.campaign_id, DeviceEvent.device_id)
            .subquery()
        )
        retries.set(
            await session.scalar(select(func.coalesce(func.sum(nudge_counts.c.retries), 0)))
        )
    for state in STATES:
        devices.labels(state).set(values.get(state, 0))
    slots.set(sum(values.get(state, 0) for state in SLOTS))
    return Response(
        generate_latest() + generate_latest(registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
    )
