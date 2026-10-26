"""Opt-in correction: consume commands returned to duplicated inform sessions."""

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from scripts import fw_simulator as reference


async def duplicate_post(path: str, body: reference.JsonObject) -> None:
    await asyncio.sleep(reference.rng.uniform(0.1, 5))
    if reference.client is None:
        return
    try:
        response = await reference.client.post(f"{reference.CONTROLLER_URL}{path}", json=body)
        response.raise_for_status()
        device = reference.devices.get(body.get("deviceId"))
        if device is None or path != f"/devices/{device['id']}/inform":
            return
        payload = response.json() if response.content else {}
        if not isinstance(payload, dict):
            raise ValueError("Controller response must be a JSON object")
        for command in payload.get("commands", []):
            if isinstance(command, dict) and command.get("type") == "DOWNLOAD":
                reference.spawn(reference.handle_download(device, command))
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[sim] Duplicate POST {path} failed: {exc!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    original = reference.duplicate_post
    reference.duplicate_post = duplicate_post
    try:
        async with reference.lifespan(app):
            yield
    finally:
        reference.duplicate_post = original


app = FastAPI(title="Supplied simulator with duplicate-response correction", lifespan=lifespan)
app.add_api_route(
    "/devices/{device_id}/connection-request",
    reference.connection_request,
    methods=["POST"],
    status_code=202,
)
app.add_api_route("/devices", reference.list_devices, methods=["GET"])
app.add_api_route("/_admin/summary", reference.admin_summary, methods=["GET"])
