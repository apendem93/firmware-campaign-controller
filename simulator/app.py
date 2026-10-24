"""Seeded gateway behaviors implementing the assignment's external HTTP contract."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import logging
import os
import random
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

logger = logging.getLogger("simulator")


@dataclass(frozen=True)
class Settings:
    controller_url: str = "http://api:8000"
    device_count: int = 1000
    seed: int = 42
    time_scale: float = 1.0

    def __post_init__(self) -> None:
        if not 1 <= self.device_count <= 10_000:
            raise ValueError("DEVICE_COUNT must be between 1 and 10000")
        if not 0 < self.time_scale <= 100:
            raise ValueError("SIMULATOR_TIME_SCALE must be greater than 0 and at most 100")
        if not self.controller_url.startswith(("http://", "https://")):
            raise ValueError("CONTROLLER_URL must be an HTTP(S) URL")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            controller_url=os.getenv("CONTROLLER_URL", "http://api:8000").rstrip("/"),
            device_count=int(os.getenv("DEVICE_COUNT", "1000")),
            seed=int(os.getenv("SIMULATOR_SEED", "42")),
            time_scale=float(os.getenv("SIMULATOR_TIME_SCALE", "1")),
        )


@dataclass
class Device:
    device_id: str
    scenario: str
    reboot_seconds: float
    callback_seconds: float
    periodic_seconds: float
    periodic_offset: float
    firmware: str = "1.0.0"
    active_key: str | None = None
    # Retaining command identities models a device that deduplicates commands.
    # The controller must not rely on this local-simulator implementation detail.
    command_status: dict[str, str] = field(default_factory=dict)
    nudge_pending: bool = False
    periodic_pending: bool = False
    downloads_started: int = 0


def build_fleet(settings: Settings) -> dict[str, Device]:
    fleet = {}
    for number in range(1, settings.device_count + 1):
        device_id = f"device-{number:06d}"
        digest = hashlib.sha256(f"{settings.seed}:{device_id}".encode()).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        bucket = rng.randrange(100)
        scenario = next(
            name
            for cutoff, name in (
                (70, "normal"),
                (75, "offline"),
                (80, "reject"),
                (85, "fault"),
                (90, "boot_only"),
                (95, "old_boot"),
                (98, "duplicate"),
                (100, "late"),
            )
            if bucket < cutoff
        )
        period = rng.uniform(180, 300)
        fleet[device_id] = Device(
            device_id=device_id,
            scenario=scenario,
            reboot_seconds=rng.uniform(5, 240),
            callback_seconds=rng.uniform(0.05, 1.0),
            periodic_seconds=period,
            periodic_offset=rng.uniform(1, period),
        )
    return fleet


class Simulator:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.devices = build_fleet(settings)
        self.tasks: set[asyncio.Task] = set()
        self.client: httpx.AsyncClient | None = None
        self.transport = transport
        self.callback_slots = asyncio.Semaphore(100)

    async def start(self, *, periodic: bool = True) -> None:
        self.client = httpx.AsyncClient(
            base_url=self.settings.controller_url,
            timeout=10,
            transport=self.transport,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
            trust_env=False,
        )
        if periodic:
            self.spawn(self.periodic_loop())

    async def stop(self) -> None:
        while self.tasks:
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            await self.client.aclose()

    def spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.task_finished)

    def task_finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            error = task.exception()
            logger.error(
                "Simulator task failed", exc_info=(type(error), error, error.__traceback__)
            )

    async def pause(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self.settings.time_scale)

    async def callback(self, path: str, body: dict) -> dict | None:
        assert self.client is not None
        for attempt in range(5):
            try:
                async with self.callback_slots:
                    response = await self.client.post(path, json=body)
                if response.is_success:
                    if not response.content:
                        return {}
                    value = response.json()
                    return value if isinstance(value, dict) else {}
                if response.status_code < 500 and response.status_code != 429:
                    logger.warning(
                        json.dumps(
                            {
                                "event": "callback_rejected",
                                "path": path,
                                "status": response.status_code,
                            }
                        )
                    )
                    return None
            except (httpx.HTTPError, ValueError):
                pass
            if attempt < 4:
                # Transport recovery runs on real time, even in accelerated demos.
                await asyncio.sleep(min(2**attempt, 5))
        logger.warning(json.dumps({"event": "callback_exhausted", "path": path}))
        return None

    async def inform(self, device: Device, event: str, transfer: dict | None = None) -> None:
        payload: dict[str, Any] = {
            "deviceId": device.device_id,
            "firmwareVersion": device.firmware,
            "event": event,
        }
        if transfer is not None:
            payload["transferComplete"] = transfer
        reply = await self.callback(f"/devices/{device.device_id}/inform", payload)
        for command in (reply or {}).get("commands", []):
            if isinstance(command, dict) and command.get("type") == "DOWNLOAD":
                await self.receive_download(device, command)

    async def acknowledge(
        self, device: Device, key: str, status: str, reason: str | None = None
    ) -> None:
        body = {"commandKey": key, "status": status}
        if reason:
            body["reason"] = reason
        await self.callback(f"/devices/{device.device_id}/command-response", body)

    async def receive_download(self, device: Device, command: dict) -> None:
        key = command.get("commandKey")
        target = command.get("targetVersion")
        if not isinstance(key, str) or not isinstance(target, str) or not command.get("url"):
            logger.warning("Ignoring malformed Download command")
            return
        if key in device.command_status:
            await self.acknowledge(device, key, device.command_status[key])
            return
        if device.active_key or device.scenario == "reject":
            device.command_status[key] = "REJECTED"
            await self.acknowledge(
                device,
                key,
                "REJECTED",
                "device busy" if device.active_key else "simulated rejection",
            )
            return
        device.command_status[key] = "ACCEPTED"
        device.active_key = key
        device.downloads_started += 1
        self.spawn(self.upgrade(device, key, target))

    async def upgrade(self, device: Device, key: str, target: str) -> None:
        started = datetime.now(UTC).isoformat()
        try:
            await self.acknowledge(device, key, "ACCEPTED")
            await self.pause(device.reboot_seconds)
            if device.scenario not in {"fault", "old_boot"}:
                device.firmware = target
            transfer = {
                "commandKey": key,
                "faultCode": 9002 if device.scenario == "fault" else 0,
                "startTime": started,
                "completeTime": datetime.now(UTC).isoformat(),
            }
            # The simulated download has ended before opening the result session.
            device.active_key = None
            if device.scenario in {"boot_only", "old_boot"}:
                await self.inform(device, "BOOT")
                return
            if device.scenario == "late":
                await self.inform(device, "BOOT")
                await self.pause(90)
            await self.inform(device, "TRANSFER_COMPLETE", transfer)
            if device.scenario == "duplicate":
                await self.pause(1)
                await self.inform(device, "TRANSFER_COMPLETE", transfer)
                await self.acknowledge(device, key, "ACCEPTED")
        finally:
            if device.active_key == key:
                device.active_key = None

    def nudge(self, device: Device) -> None:
        if device.scenario == "offline" or device.nudge_pending or device.active_key is not None:
            return
        device.nudge_pending = True
        self.spawn(self.nudge_callback(device))

    async def nudge_callback(self, device: Device) -> None:
        try:
            await self.pause(device.callback_seconds)
            await self.inform(device, "CONNECTION_REQUEST")
        finally:
            device.nudge_pending = False

    async def periodic_loop(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        schedule = [
            (
                now
                + d.periodic_offset
                / d.periodic_seconds
                * max(30, d.periodic_seconds * self.settings.time_scale),
                d.device_id,
            )
            for d in self.devices.values()
            if d.scenario != "offline"
        ]
        heapq.heapify(schedule)
        while schedule:
            due, device_id = heapq.heappop(schedule)
            await asyncio.sleep(max(0, due - loop.time()))
            device = self.devices[device_id]
            # A rebooting device cannot open a second session until it returns.
            if device.active_key is None and not device.periodic_pending:
                device.periodic_pending = True
                self.spawn(self.periodic_inform(device))
            heapq.heappush(
                schedule,
                (
                    loop.time() + max(30, device.periodic_seconds * self.settings.time_scale),
                    device_id,
                ),
            )

    async def periodic_inform(self, device: Device) -> None:
        try:
            await self.inform(device, "PERIODIC")
        finally:
            device.periodic_pending = False


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    simulator = Simulator(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await simulator.start()
        try:
            yield
        finally:
            await simulator.stop()

    app = FastAPI(title="Local gateway simulator", lifespan=lifespan)
    app.state.simulator = simulator

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/devices")
    async def devices(version: str | None = None):
        return [
            {"deviceId": d.device_id, "firmwareVersion": d.firmware}
            for d in simulator.devices.values()
            if version is None or d.firmware == version
        ]

    @app.post("/devices/{device_id}/connection-request", status_code=202)
    async def connection_request(device_id: str):
        device = simulator.devices.get(device_id)
        if device is None:
            raise HTTPException(404, "Unknown device")
        simulator.nudge(device)
        return {"delivered": True}

    @app.get("/summary")
    async def summary():
        return {
            "seed": settings.seed,
            "deviceCount": settings.device_count,
            "scenarios": dict(Counter(d.scenario for d in simulator.devices.values())),
            "versions": dict(Counter(d.firmware for d in simulator.devices.values())),
            "downloadsStarted": sum(d.downloads_started for d in simulator.devices.values()),
            "activeDownloads": sum(d.active_key is not None for d in simulator.devices.values()),
        }

    return app


app = create_app()
