import logging
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select

from controller.models import (
    ACTIVE,
    DISPATCHED,
    SLOTS,
    STATES,
    TERMINAL,
    Campaign,
    CampaignDevice,
    DeviceEvent,
)
from controller.observability import CALLBACKS

log = logging.getLogger(__name__)


def audit(
    session, campaign, attempt, kind, *, previous=None, reason=None, payload=None, command_key=None
):
    session.add(
        DeviceEvent(
            campaign_id=campaign.id if campaign else None,
            device_id=attempt.device_id if attempt else None,
            command_key=str(attempt.command_key)
            if attempt and attempt.command_key
            else command_key,
            kind=kind,
            previous_state=previous,
            new_state=attempt.state if attempt else None,
            reason=reason,
            payload=payload or {},
        )
    )


def transition(session, campaign, attempt, state, now, reason, evidence=None):
    previous = attempt.state
    attempt.state = state
    attempt.outcome_reason = reason
    if evidence:
        attempt.evidence = evidence
    if state in TERMINAL:
        attempt.finished_at = now
        attempt.outcome_epoch = campaign.gate_epoch
    audit(session, campaign, attempt, "transition", previous=previous, reason=reason)
    log.info(
        "device transition",
        extra={
            "fields": {
                "campaign_id": campaign.id,
                "device_id": attempt.device_id,
                "command_key": attempt.command_key,
                "from": previous,
                "to": state,
                "reason": reason,
            }
        },
    )


async def counts(session, campaign_id, epoch=None):
    query = select(CampaignDevice.state, func.count()).where(
        CampaignDevice.campaign_id == campaign_id
    )
    if epoch is not None:
        query = query.where(CampaignDevice.outcome_epoch == epoch)
    rows = await session.execute(query.group_by(CampaignDevice.state))
    values = dict(rows.all())
    return {state: values.get(state, 0) for state in STATES}


def failure_rate(values):
    failures = values["FAILED"] + values["TIMED_OUT"]
    resolved = failures + values["SUCCEEDED"]
    return 100 * failures / resolved if resolved else 0.0


async def progress(session, campaign):
    values = await counts(session, campaign.id)
    gate = await counts(session, campaign.id, campaign.gate_epoch)
    return {
        "id": str(campaign.id),
        "status": campaign.status,
        "haltReason": campaign.halt_reason,
        "targetVersion": campaign.target_version,
        "totalDevices": campaign.total,
        "counts": values,
        "slotsHeld": sum(values[s] for s in SLOTS),
        "maxConcurrent": campaign.max_concurrent,
        "resolvedCount": sum(values[s] for s in TERMINAL),
        "failureRate": failure_rate(values),
        "failureThresholdPercent": campaign.failure_threshold,
        "failureGate": {
            "epoch": campaign.gate_epoch,
            "minimumSample": campaign.gate_minimum,
            "sampleSize": sum(gate[s] for s in TERMINAL),
            "failureRate": failure_rate(gate),
        },
        "config": campaign.tuning,
        "createdAt": campaign.created_at,
        "completedAt": campaign.completed_at,
    }


async def park_unsent(session, campaign, now):
    attempts = (
        await session.scalars(
            select(CampaignDevice).where(
                CampaignDevice.campaign_id == campaign.id, CampaignDevice.state == "NOTIFIED"
            )
        )
    ).all()
    for attempt in attempts:
        transition(session, campaign, attempt, "PENDING", now, "DISPATCH_PAUSED")
        attempt.lease_token = None
        attempt.lease_until = None
        attempt.inform_deadline = None


async def halt(session, campaign, now, reason):
    if campaign.status == "COMPLETED":
        return
    changed = campaign.status != "HALTED" or campaign.halt_reason != reason
    campaign.status = "HALTED"
    campaign.halt_reason = reason
    if changed:
        audit(session, campaign, None, "halt", reason=reason)
    await park_unsent(session, campaign, now)


async def evaluate(session, campaign, now):
    await session.flush()
    values = await counts(session, campaign.id)
    if sum(values[s] for s in TERMINAL) == campaign.total:
        if campaign.status != "COMPLETED":
            campaign.status = "COMPLETED"
            campaign.completed_at = now
            audit(session, campaign, None, "completed")
        return
    if values["OUTCOME_UNKNOWN"]:
        await halt(session, campaign, now, "OUTCOME_UNKNOWN")
        return
    gate = await counts(session, campaign.id, campaign.gate_epoch)
    if (
        campaign.status in ("RUNNING", "PAUSED")
        and sum(gate[s] for s in TERMINAL) >= campaign.gate_minimum
        and failure_rate(gate) > campaign.failure_threshold
    ):
        await halt(session, campaign, now, "FAILURE_THRESHOLD")


async def lock_campaign(session, campaign_id):
    campaign = await session.scalar(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    )
    if campaign is None:
        raise HTTPException(404, "Unknown campaign")
    return campaign


async def lookup_key(session, key):
    try:
        parsed = uuid.UUID(key)
    except ValueError:
        return None
    return await session.scalar(select(CampaignDevice).where(CampaignDevice.command_key == parsed))


async def keyed_attempt(session, device_id, key):
    reference = await lookup_key(session, key)
    if reference is None:
        return None, None
    if reference.device_id != device_id:
        raise HTTPException(409, "commandKey belongs to another device")
    campaign = await lock_campaign(session, reference.campaign_id)
    # Reload after acquiring the campaign lock: the reference may have been read before a competing commit.
    await session.refresh(reference)
    return campaign, reference


async def active_attempt(session, device_id):
    campaign_id = await session.scalar(
        select(CampaignDevice.campaign_id)
        .join(Campaign)
        .where(
            Campaign.status.in_(ACTIVE),
            CampaignDevice.device_id == device_id,
            CampaignDevice.state.in_(SLOTS),
        )
    )
    if campaign_id is None:
        return None, None
    campaign = await lock_campaign(session, campaign_id)
    attempt = await session.scalar(
        select(CampaignDevice).where(
            CampaignDevice.campaign_id == campaign.id, CampaignDevice.device_id == device_id
        )
    )
    return campaign, attempt


async def process_ack(session, campaign, attempt, body, now):
    audit(session, campaign, attempt, "ack", payload=body.model_dump(mode="json"))
    if attempt.state in TERMINAL:
        CALLBACKS.labels("late_ack").inc()
        return
    if attempt.ack_status == body.status:
        CALLBACKS.labels("duplicate_ack").inc()
        return
    if attempt.ack_status in ("ACCEPTED", "REJECTED"):
        CALLBACKS.labels("contradiction").inc()
        audit(session, campaign, attempt, "contradiction", reason="CONFLICTING_ACK")
        return
    attempt.ack_status = body.status
    if body.status == "REJECTED":
        transition(session, campaign, attempt, "FAILED", now, "COMMAND_REJECTED", "ack")
    elif attempt.state == "DOWNLOAD_ISSUED":
        transition(session, campaign, attempt, "AWAITING_RESULT", now, "COMMAND_ACCEPTED")
    await evaluate(session, campaign, now)


async def process_inform(session, campaign, attempt, body, now):
    audit(session, campaign, attempt, "inform", payload=body.model_dump(mode="json"))
    tc = body.transferComplete
    if attempt.state == "NOTIFIED" and attempt.inform_deadline <= now:
        transition(session, campaign, attempt, "TIMED_OUT", now, "NO_SESSION", "deadline")
        await evaluate(session, campaign, now)
        return []
    if attempt.state in TERMINAL:
        if tc:
            contradictory = (attempt.state == "SUCCEEDED" and tc.faultCode != 0) or (
                attempt.state != "SUCCEEDED" and tc.faultCode == 0
            )
            CALLBACKS.labels("contradiction" if contradictory else "duplicate_completion").inc()
            if contradictory:
                audit(session, campaign, attempt, "contradiction", reason="LATE_TERMINAL_EVIDENCE")
        return []
    if tc:
        if attempt.completion_fault is not None:
            CALLBACKS.labels(
                "duplicate_completion"
                if attempt.completion_fault == tc.faultCode
                else "contradiction"
            ).inc()
            if attempt.completion_fault != tc.faultCode:
                audit(session, campaign, attempt, "contradiction", reason="CONFLICTING_COMPLETION")
                return []
        else:
            attempt.completion_fault = tc.faultCode
        if tc.faultCode != 0:
            transition(
                session,
                campaign,
                attempt,
                "FAILED",
                now,
                f"TRANSFER_FAULT_{tc.faultCode}",
                "transfer_complete",
            )
        elif body.firmwareVersion == campaign.target_version:
            transition(
                session,
                campaign,
                attempt,
                "SUCCEEDED",
                now,
                "TARGET_CONFIRMED",
                "transfer_complete",
            )
        else:
            if attempt.verification_deadline is None:
                attempt.verification_deadline = now + timedelta(
                    seconds=campaign.tuning["verification_grace"]
                )
            if attempt.state != "AWAITING_RESULT":
                transition(session, campaign, attempt, "AWAITING_RESULT", now, "VERIFYING_VERSION")
    elif body.event == "BOOT" and attempt.state in DISPATCHED:
        attempt.boot_version = body.firmwareVersion
        attempt.boot_at = now
        if body.firmwareVersion == campaign.target_version:
            transition(session, campaign, attempt, "SUCCEEDED", now, "TARGET_BOOT", "boot_inferred")
        elif attempt.state == "OUTCOME_UNKNOWN":
            transition(
                session, campaign, attempt, "FAILED", now, "BOOT_OLD_VERSION", "boot_inferred"
            )
    await evaluate(session, campaign, now)
    if campaign.status != "RUNNING" or attempt.state != "NOTIFIED":
        return []
    # Only an inform without historical completion evidence can authorize a new command.
    if tc is not None:
        return []
    attempt.command_key = uuid.uuid4()
    attempt.dispatched_at = now
    attempt.ack_deadline = now + timedelta(seconds=campaign.tuning["ack_timeout"])
    attempt.result_deadline = now + timedelta(seconds=campaign.tuning["result_timeout"])
    attempt.lease_token = None
    attempt.lease_until = None
    transition(session, campaign, attempt, "DOWNLOAD_ISSUED", now, "COMMAND_AUTHORIZED")
    return [
        {
            "type": "DOWNLOAD",
            "commandKey": str(attempt.command_key),
            "url": campaign.image_url,
            "targetVersion": campaign.target_version,
        }
    ]


async def tick(session, now):
    campaign = await session.scalar(
        select(Campaign).where(Campaign.status.in_(ACTIVE)).with_for_update(skip_locked=True)
    )
    if campaign is None:
        return []
    attempts = (
        await session.scalars(
            select(CampaignDevice).where(
                CampaignDevice.campaign_id == campaign.id, CampaignDevice.state.in_(SLOTS)
            )
        )
    ).all()
    for attempt in attempts:
        if attempt.state == "NOTIFIED" and attempt.inform_deadline <= now:
            transition(session, campaign, attempt, "TIMED_OUT", now, "NO_SESSION", "deadline")
        elif attempt.state in DISPATCHED and attempt.state != "OUTCOME_UNKNOWN":
            if attempt.verification_deadline:
                if attempt.verification_deadline <= now:
                    transition(
                        session,
                        campaign,
                        attempt,
                        "FAILED",
                        now,
                        "VERSION_MISMATCH",
                        "transfer_complete",
                    )
            elif attempt.result_deadline <= now:
                if attempt.boot_at is not None:
                    transition(
                        session,
                        campaign,
                        attempt,
                        "FAILED",
                        now,
                        "BOOT_OLD_VERSION",
                        "boot_inferred",
                    )
                else:
                    transition(
                        session,
                        campaign,
                        attempt,
                        "OUTCOME_UNKNOWN",
                        now,
                        "RESULT_DEADLINE",
                        "deadline",
                    )
            elif attempt.state == "DOWNLOAD_ISSUED" and attempt.ack_deadline <= now:
                attempt.ack_status = "MISSING"
                transition(session, campaign, attempt, "AWAITING_RESULT", now, "ACK_DEADLINE")
    await evaluate(session, campaign, now)
    if campaign.status != "RUNNING":
        return []
    values = await counts(session, campaign.id)
    available = campaign.max_concurrent - sum(values[s] for s in SLOTS)
    nudge_ready = (
        CampaignDevice.campaign_id == campaign.id,
        CampaignDevice.state == "NOTIFIED",
        CampaignDevice.next_nudge_at <= now,
        (CampaignDevice.lease_until.is_(None) | (CampaignDevice.lease_until <= now)),
        CampaignDevice.nudge_attempts < campaign.tuning["nudge_attempts"],
    )
    due_count = await session.scalar(
        select(func.count()).select_from(CampaignDevice).where(*nudge_ready)
    )
    # Start the session deadline only when a nudge can be claimed in this batch.
    available = min(available, max(0, campaign.tuning["max_http_concurrency"] - due_count))
    pending = (
        await session.scalars(
            select(CampaignDevice)
            .where(CampaignDevice.campaign_id == campaign.id, CampaignDevice.state == "PENDING")
            .order_by(CampaignDevice.device_id)
            .limit(max(0, available))
        )
    ).all()
    for attempt in pending:
        transition(session, campaign, attempt, "NOTIFIED", now, "SLOT_RESERVED")
        attempt.nudge_attempts = 0
        attempt.next_nudge_at = now
        attempt.inform_deadline = now + timedelta(seconds=campaign.tuning["inform_timeout"])
    await session.flush()
    due = (
        await session.scalars(
            select(CampaignDevice)
            .where(*nudge_ready)
            .order_by(CampaignDevice.next_nudge_at, CampaignDevice.device_id)
            .limit(campaign.tuning["max_http_concurrency"])
        )
    ).all()
    jobs = []
    for attempt in due:
        attempt.lease_token = uuid.uuid4()
        attempt.lease_until = now + timedelta(seconds=campaign.tuning["lease_seconds"])
        attempt.nudge_attempts += 1
        jobs.append(
            {
                "campaign_id": campaign.id,
                "attempt_id": attempt.id,
                "device_id": attempt.device_id,
                "token": attempt.lease_token,
                "timeout": campaign.tuning["http_timeout"],
            }
        )
    return jobs
