import asyncio
import logging
import random
import signal
import time
from datetime import timedelta
from urllib.parse import quote

import httpx
from prometheus_client import start_http_server

from controller.config import settings
from controller.db import db_now, engine, sessions
from controller.models import CampaignDevice
from controller.observability import HEARTBEAT, configure_logging, outbound
from controller.service import audit, lock_campaign, tick

log = logging.getLogger(__name__)


async def send_nudge(client, job):
    # Recheck committed intent just before network work. A simultaneous pause can still
    # allow a harmless nudge, but the inform handler will not authorize a Download.
    async with sessions.begin() as session:
        campaign = await lock_campaign(session, job["campaign_id"])
        attempt = await session.get(CampaignDevice, job["attempt_id"])
        if (
            campaign.status != "RUNNING"
            or attempt.state != "NOTIFIED"
            or attempt.lease_token != job["token"]
        ):
            return
    outcome = "transport_error"
    retry_after = None
    try:
        response = await outbound(
            client,
            "POST",
            settings().simulator_url.rstrip("/")
            + "/devices/"
            + quote(job["device_id"], safe="")
            + "/connection-request",
            "connection_request",
            timeout=job["timeout"],
        )
        outcome = str(response.status_code)
        if response.status_code == 429:
            try:
                retry_after = min(float(response.headers.get("Retry-After", "0")), 60)
            except ValueError:
                pass
    except httpx.HTTPError:
        pass
    async with sessions.begin() as session:
        campaign = await lock_campaign(session, job["campaign_id"])
        attempt = await session.get(CampaignDevice, job["attempt_id"])
        audit(session, campaign, attempt, "nudge_response", reason=outcome)
        if attempt.state != "NOTIFIED" or attempt.lease_token != job["token"]:
            return
        now = await db_now(session)
        delay = (
            campaign.tuning["nudge_backoff"]
            * 2 ** (attempt.nudge_attempts - 1)
            * random.uniform(0.8, 1.2)
        )
        attempt.next_nudge_at = now + timedelta(seconds=max(delay, retry_after or 0))
        attempt.lease_token = None
        attempt.lease_until = None


async def schedule_once(client):
    async with sessions.begin() as session:
        jobs = await tick(session, await db_now(session))
    # Leases are claimed in a bounded batch, so no queued job waits behind an unbounded semaphore.
    await asyncio.gather(*(send_nudge(client, job) for job in jobs))
    HEARTBEAT.set(time.time())


async def run():
    configure_logging()
    tuning = settings().tuning()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    start_http_server(settings().worker_metrics_port)
    async with httpx.AsyncClient(
        timeout=tuning.http_timeout,
        limits=httpx.Limits(max_connections=tuning.max_http_concurrency),
    ) as client:
        while not stop.is_set():
            try:
                await schedule_once(client)
            except Exception:
                log.exception("scheduler iteration failed; durable work will be retried")
            try:
                await asyncio.wait_for(stop.wait(), timeout=tuning.scheduler_interval)
            except TimeoutError:
                pass
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
