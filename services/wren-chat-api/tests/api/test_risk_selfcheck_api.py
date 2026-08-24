"""API contract tests for POST /v1/risk/self-check."""

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from wren_chat_api.app import create_app
from wren_chat_api.config import Settings
from wren_chat_api.contracts import RiskSelfCheckRequest, RiskSelfCheckResponse
from wren_chat_api.errors import (
    InvalidRiskCheckResult,
    RequestTimedOut,
    UpstreamFailed,
)

_BODY = {
    "component": "struts2",
    "version": "2.3.31",
    "vulnerability_descriptions": [
        "Apache Struts2 远程代码执行漏洞（CVE-2017-5638），受影响版本 Struts "
        "2.3.5 - 2.3.31、2.5 - 2.5.10。",
        "Spring Data Commons 反序列化漏洞，受影响版本 1.13 - 1.13.11。",
    ],
}


class FakeRiskSelfCheckService:
    def __init__(
        self,
        error: Exception | None = None,
        result: RiskSelfCheckResponse | None = None,
    ):
        self.error = error
        self.result = result
        self.calls: list[RiskSelfCheckRequest] = []

    async def check(self, request: RiskSelfCheckRequest) -> RiskSelfCheckResponse:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result.model_copy(update={"component": request.component})
        # Echo a hit verdict so both response fields are asserted per call.
        return RiskSelfCheckResponse(component=request.component, matched=1)


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "state_database_url": "postgresql://user:pass@localhost:5432/wren_test",
        "api_key": "test-key",
        "project_path": tmp_path,
        "model": "test-model",
    }
    values.update(overrides)
    return Settings(**values, _env_file=None)


def make_app(
    tmp_path, *, error: Exception | None = None, result=None, **settings_overrides
) -> tuple[FastAPI, FakeRiskSelfCheckService]:
    service = FakeRiskSelfCheckService(error, result)
    app = create_app(
        make_settings(tmp_path, **settings_overrides),
        overrides={"risk_selfcheck_service": service},
    )
    return app, service


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_selfcheck_success_returns_component_and_verdict(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk/self-check",
            headers={"Authorization": "Bearer test-key"},
            json=_BODY,
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"component", "matched"}
    assert body["component"] == "struts2"
    assert body["matched"] in (0, 1)
    assert body["matched"] == 1


async def test_selfcheck_no_hit_returns_zero(tmp_path):
    app, _ = make_app(
        tmp_path, result=RiskSelfCheckResponse(component="x", matched=0)
    )
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk/self-check",
            headers={"Authorization": "Bearer test-key"},
            json=_BODY,
        )

    assert response.status_code == 200
    assert response.json() == {"component": "struts2", "matched": 0}


async def test_selfcheck_service_receives_validated_request(tmp_path):
    app, service = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk/self-check",
            headers={"Authorization": "Bearer test-key"},
            json={
                "component": "  log4j  ",
                "version": "2.14.1",
                "vulnerability_descriptions": [" Apache Log4j2 JNDI 注入漏洞 "],
            },
        )

    assert response.status_code == 200
    assert response.json()["component"] == "log4j"
    assert len(service.calls) == 1
    # Whitespace is stripped before the request reaches the service, so the
    # model prompt never carries blank-padded identifiers.
    assert service.calls[0].component == "log4j"
    assert service.calls[0].vulnerability_descriptions == [
        "Apache Log4j2 JNDI 注入漏洞"
    ]


async def test_wrong_api_key_returns_401(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk/self-check",
            headers={"Authorization": "Bearer wrong"},
            json=_BODY,
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"


async def test_missing_authorization_returns_401(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post("/v1/risk/self-check", json=_BODY)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {},  # missing everything
        {"component": "struts2"},  # missing version and descriptions
        {
            "component": "struts2",
            "version": "2.3.31",
            "vulnerability_descriptions": [],
        },  # descriptions must not be empty
        {
            "component": "struts2",
            "version": "2.3.31",
            "vulnerability_descriptions": ["   "],
        },  # blank description rejected
        {
            "component": "struts2",
            "version": "2.3.31",
            "vulnerability_descriptions": ["x"] * 51,
        },  # over the 50-description ceiling
        {
            "component": "struts2",
            "version": "2.3.31",
            "vulnerability_descriptions": ["x"],
            "extra": "field",
        },  # unknown field: extra=forbid
    ],
)
async def test_invalid_bodies_return_400(tmp_path, body):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk/self-check",
            headers={"Authorization": "Bearer test-key"},
            json=body,
        )

    assert response.status_code == 400
    error_body = response.json()["error"]
    assert error_body["code"] == "INVALID_REQUEST"
    assert "component" in error_body["message"]


@pytest.mark.parametrize(
    "error,expected_status,expected_code",
    [
        (UpstreamFailed(), 502, "UPSTREAM_FAILED"),
        (InvalidRiskCheckResult(), 502, "INVALID_RISK_CHECK_RESULT"),
        (RequestTimedOut(), 504, "REQUEST_TIMED_OUT"),
    ],
)
async def test_service_errors_map_to_stable_http_responses(
    tmp_path, error, expected_status, expected_code
):
    app, _ = make_app(tmp_path, error=error)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk/self-check",
            headers={"Authorization": "Bearer test-key"},
            json=_BODY,
        )

    assert response.status_code == expected_status
    assert response.json() == {
        "error": {"code": expected_code, "message": error.public_message}
    }
