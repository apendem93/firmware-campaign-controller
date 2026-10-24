"""Integration fixtures use only a dedicated, disposable PostgreSQL database."""

import os
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.engine import make_url

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://firmware:firmware@127.0.0.1:5432/firmware_test",
)
if make_url(TEST_DATABASE_URL).database != "firmware_test":
    raise RuntimeError("TEST_DATABASE_URL must name the disposable firmware_test database")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


@pytest.fixture
async def database():
    from controller.db import engine, sessions
    from controller.models import Base

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield sessions
    await engine.dispose()


@pytest.fixture
async def client(database) -> AsyncIterator[httpx.AsyncClient]:
    from controller.api import app

    fleet = [{"deviceId": f"device-{index}", "firmwareVersion": "1.0.0"} for index in range(1, 7)]
    calls = []

    def simulator(request):
        calls.append(request)
        if request.method == "GET" and request.url.path == "/devices":
            return httpx.Response(200, json=fleet)
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(simulator)) as outbound:
        app.state.http = outbound
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://controller"
        ) as incoming:
            incoming.fleet = fleet
            incoming.simulator_calls = calls
            yield incoming
