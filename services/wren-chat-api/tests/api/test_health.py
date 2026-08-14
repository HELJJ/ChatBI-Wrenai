"""API contract tests for health endpoints."""

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from wren_chat_api.app import create_app
from wren_chat_api.config import Settings


def make_settings(tmp_path) -> Settings:
    return Settings(
        state_database_url="postgresql://user:pass@localhost:5432/wren_test",
        api_key="test-key",
        project_path=tmp_path,
        model="test-model",
    )


@pytest.fixture
def app(tmp_path) -> FastAPI:
    async def ready() -> None:
        return None

    return create_app(
        make_settings(tmp_path),
        overrides={"readiness": ready},
    )


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


async def test_liveness_is_unauthenticated(client):
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_returns_ready_when_check_passes(client):
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_readiness_returns_503_when_check_fails(tmp_path):
    async def failing() -> None:
        raise RuntimeError("database unreachable")

    app = create_app(
        make_settings(tmp_path),
        overrides={"readiness": failing},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        response = await async_client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
