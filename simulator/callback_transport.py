"""Bound callback concurrency before HTTPX pooling; retry transient delivery failures."""

import asyncio
import copy
import logging
import math
import random
import time
from contextlib import AsyncExitStack
from datetime import UTC
from email.utils import parsedate_to_datetime

import httpx
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class CallbackSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIM_CALLBACK_", extra="ignore")
    periodic_concurrency: int = Field(default=16, ge=1, le=128)
    critical_concurrency: int = Field(default=32, ge=1, le=128)
    max_attempts: int = Field(default=3, ge=1, le=10)
    request_timeout: float = Field(default=5, gt=0, le=60)
    retry_base_delay: float = Field(default=0.2, gt=0, le=10)
    max_retry_delay: float = Field(default=2, gt=0, le=30)


def callback_kind(path, body):
    if path.endswith("/command-response"):
        return "ack"
    event = body.get("event")
    return {
        "PERIODIC": "periodic",
        "CONNECTION_REQUEST": "connection_request",
        "BOOT": "boot",
        "TRANSFER_COMPLETE": "transfer_complete",
    }.get(event, "other")


def retry_after_seconds(value):
    if value is None:
        return 0.0
    try:
        delay = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            delay = deadline.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return 0.0 if math.isnan(delay) else max(0.0, delay)


class CallbackTransport:
    def __init__(self, base_url, settings=None, *, transport=None):
        self.base_url = base_url.rstrip("/")
        self.settings = settings or CallbackSettings()
        self.transport = transport
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "simulator_callback_requests_total",
            "Callback HTTP attempts",
            ["kind", "status", "outcome"],
            registry=self.registry,
        )
        self.retries = Counter(
            "simulator_callback_retries_total",
            "Transient callback retries",
            ["kind"],
            registry=self.registry,
        )
        self.exhausted = Counter(
            "simulator_callback_exhausted_total",
            "Callbacks without a successful response",
            ["kind"],
            registry=self.registry,
        )
        buckets = (0.005, 0.025, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)
        self.queue_wait = Histogram(
            "simulator_callback_queue_seconds",
            "Wait before entering an HTTP pool",
            ["traffic"],
            buckets=buckets,
            registry=self.registry,
        )
        self.duration = Histogram(
            "simulator_callback_request_seconds",
            "Callback HTTP attempt duration",
            ["kind"],
            buckets=buckets,
            registry=self.registry,
        )
        self.inflight = Gauge(
            "simulator_callback_inflight",
            "Admitted callback HTTP attempts",
            ["traffic"],
            registry=self.registry,
        )
        self.queued = Gauge(
            "simulator_callback_queued",
            "Callbacks waiting for admission",
            ["traffic"],
            registry=self.registry,
        )
        self.budgets = {
            "periodic": self.settings.periodic_concurrency,
            "critical": self.settings.critical_concurrency,
        }
        self.semaphores = {name: asyncio.Semaphore(n) for name, n in self.budgets.items()}
        self.clients = {}
        self.stack = AsyncExitStack()
        # Transport backoff must not consume the supplied simulator's profile/timing RNG.
        self.jitter = random.Random(0)

    async def __aenter__(self):
        try:
            for traffic, budget in self.budgets.items():
                self.clients[traffic] = await self.stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=self.base_url,
                        timeout=self.settings.request_timeout,
                        limits=httpx.Limits(
                            max_connections=budget,
                            max_keepalive_connections=budget,
                            keepalive_expiry=4,
                        ),
                        transport=self.transport,
                    )
                )
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        await self.stack.aclose()

    async def send(self, path, body):
        # Freeze evidence, timestamps, and command identity across all delivery attempts.
        body = copy.deepcopy(body)
        kind = callback_kind(path, body)
        traffic = "periodic" if kind == "periodic" else "critical"
        for attempt in range(self.settings.max_attempts):
            queued_at = time.monotonic()
            self.queued.labels(traffic).inc()
            try:
                await self.semaphores[traffic].acquire()
            finally:
                self.queued.labels(traffic).dec()
                self.queue_wait.labels(traffic).observe(time.monotonic() - queued_at)
            retry_after = 0.0
            retryable = False
            started = time.monotonic()
            self.inflight.labels(traffic).inc()
            try:
                # HTTPX timeouts are per phase; this also bounds each whole network attempt.
                async with asyncio.timeout(self.settings.request_timeout):
                    response = await self.clients[traffic].post(path, json=body)
                status = str(response.status_code)
                if response.is_success:
                    payload = response.json() if response.content else {}
                    if not isinstance(payload, dict):
                        raise ValueError("Controller response must be a JSON object")
                    self.requests.labels(kind, status, "success").inc()
                    return payload
                self.requests.labels(kind, status, "http_error").inc()
                retryable = response.status_code == 429 or response.status_code >= 500
                retry_after = retry_after_seconds(response.headers.get("Retry-After"))
            except (httpx.TransportError, TimeoutError) as exc:
                self.requests.labels(kind, "none", "transport_error").inc()
                retryable = True
                log.warning("Callback %s delivery failed: %r", path, exc)
            except ValueError as exc:
                self.requests.labels(kind, "none", "invalid_response").inc()
                log.warning("Callback %s response invalid: %r", path, exc)
            finally:
                self.duration.labels(kind).observe(time.monotonic() - started)
                self.inflight.labels(traffic).dec()
                self.semaphores[traffic].release()
            if not retryable or attempt + 1 == self.settings.max_attempts:
                break
            if retry_after > self.settings.max_retry_delay:
                log.error(
                    "Callback %s Retry-After %.3fs exceeds retry budget %.3fs; not retrying early",
                    path,
                    retry_after,
                    self.settings.max_retry_delay,
                )
                break
            self.retries.labels(kind).inc()
            backoff = self.settings.retry_base_delay * 2**attempt * self.jitter.uniform(0.8, 1.2)
            await asyncio.sleep(max(retry_after, min(self.settings.max_retry_delay, backoff)))
        self.exhausted.labels(kind).inc()
        log.error("Callback %s exhausted delivery attempts", path)
        return None
