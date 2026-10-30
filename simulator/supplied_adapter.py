"""Opt-in simulator corrections: bounded, retried callbacks and duplicate response handling."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from scripts import fw_simulator as reference
from simulator.callback_transport import CallbackTransport

callbacks: CallbackTransport | None = None


async def post(path: str, body: reference.JsonObject) -> reference.JsonObject | None:
    if callbacks is None:
        raise RuntimeError("Callback transport is not initialized")
    payload = await callbacks.send(path, body)
    if payload is not None and reference.rng.random() < reference.P_DUPLICATE:
        reference.spawn(duplicate_post(path, body))
    return payload


async def duplicate_post(path: str, body: reference.JsonObject) -> None:
    await asyncio.sleep(reference.rng.uniform(0.1, 5))
    if callbacks is None:
        return
    # A duplicate can be the first session after admission. Consume its response too.
    payload = await callbacks.send(path, body)
    device = reference.devices.get(body.get("deviceId"))
    if device is None or path != f"/devices/{device['id']}/inform":
        return
    for command in (payload or {}).get("commands", []):
        if isinstance(command, dict) and command.get("type") == "DOWNLOAD":
            reference.spawn(reference.handle_download(device, command))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global callbacks
    original_post, original_duplicate, original_callbacks = (
        reference.post,
        reference.duplicate_post,
        callbacks,
    )
    async with CallbackTransport(reference.CONTROLLER_URL) as transport:
        callbacks = transport
        reference.post, reference.duplicate_post = post, duplicate_post
        try:
            # The supplied lifespan cancels and joins device tasks before clients close.
            async with reference.lifespan(app):
                yield
        finally:
            reference.post, reference.duplicate_post = original_post, original_duplicate
            callbacks = original_callbacks


app = FastAPI(title="Supplied simulator with callback corrections", lifespan=lifespan)
app.add_api_route(
    "/devices/{device_id}/connection-request",
    reference.connection_request,
    methods=["POST"],
    status_code=202,
)
app.add_api_route("/devices", reference.list_devices, methods=["GET"])
app.add_api_route("/_admin/summary", reference.admin_summary, methods=["GET"])


@app.get("/metrics")
async def metrics():
    return Response(
        generate_latest(callbacks.registry) if callbacks else b"",
        media_type=CONTENT_TYPE_LATEST,
    )
