"""API contract tests for POST /v1/chat."""

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from wren_chat_api.app import create_app
from wren_chat_api.config import Settings
from wren_chat_api.contracts import ChatRequest, ChatResponse
from wren_chat_api.errors import (
    CapacityExceeded,
    ChatServiceError,
    InvalidFinalAnswer,
    PersistenceFailed,
    QuestionUnanswerable,
    RequestTimedOut,
    SessionBusy,
    SessionLeaseLost,
    UpstreamFailed,
)


class FakeChatService:
    def __init__(self, answer: str = "42", error: Exception | None = None):
        self.answer = answer
        self.error = error
        self.calls: list[ChatRequest] = []

    async def ask(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return ChatResponse(session_id=request.session_id, answer=self.answer)


def make_settings(tmp_path) -> Settings:
    return Settings(
        state_database_url="postgresql://user:pass@localhost:5432/wren_test",
        api_key="test-key",
        project_path=tmp_path,
        model="test-model",
    )


@pytest.fixture
def app(tmp_path) -> FastAPI:
    return create_app(
        make_settings(tmp_path),
        overrides={"chat_service": FakeChatService()},
    )


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


async def test_chat_success_has_exactly_two_fields(client):
    response = await client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer test-key"},
        json={"session_id": "s-1", "question": "count orders"},
    )

    assert response.status_code == 200
    assert response.json() == {"session_id": "s-1", "answer": "42"}


async def test_invalid_key_returns_generic_401(client):
    response = await client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer wrong"},
        json={"session_id": "s-1", "question": "count orders"},
    )

    assert response.status_code == 401
    assert "wrong" not in response.text
    assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"


async def test_missing_authorization_returns_401(client):
    response = await client.post(
        "/v1/chat",
        json={"session_id": "s-1", "question": "count orders"},
    )

    assert response.status_code == 401


async def test_unknown_request_field_is_rejected(client):
    response = await client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer test-key"},
        json={"session_id": "s-1", "question": "count", "sql": "SELECT 1"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


async def test_missing_or_invalid_fields_are_rejected(client):
    response = await client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer test-key"},
        json={"question": "missing session"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "error,expected_status,expected_code",
    [
        (SessionBusy(), 409, "SESSION_BUSY"),
        (SessionLeaseLost(), 409, "SESSION_LEASE_LOST"),
        (QuestionUnanswerable(), 422, "QUESTION_UNANSWERABLE"),
        (CapacityExceeded(), 429, "CAPACITY_EXCEEDED"),
        (PersistenceFailed(), 500, "PERSISTENCE_FAILED"),
        (UpstreamFailed(), 502, "UPSTREAM_FAILED"),
        (InvalidFinalAnswer(), 502, "INVALID_FINAL_ANSWER"),
        (RequestTimedOut(), 504, "REQUEST_TIMED_OUT"),
    ],
)
async def test_service_errors_map_to_stable_http_responses(
    tmp_path, error, expected_status, expected_code
):
    app = create_app(
        make_settings(tmp_path),
        overrides={"chat_service": FakeChatService(error=error)},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        response = await async_client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer test-key"},
            json={"session_id": "s-1", "question": "count orders"},
        )

    assert response.status_code == expected_status
    assert response.json() == {
        "error": {
            "code": expected_code,
            "message": error.public_message,
        }
    }


async def test_unexpected_error_returns_generic_500(tmp_path):
    app = create_app(
        make_settings(tmp_path),
        overrides={"chat_service": FakeChatService(error=RuntimeError("boom"))},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        response = await async_client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer test-key"},
            json={"session_id": "s-1", "question": "count orders"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "boom" not in response.text


async def test_metrics_endpoint_requires_key(client):
    unauthorized = await client.get("/metrics")
    assert unauthorized.status_code == 401

    authorized = await client.get(
        "/metrics", headers={"Authorization": "Bearer test-key"}
    )
    assert authorized.status_code == 200
    assert "wren_chat_requests_total" in authorized.text
