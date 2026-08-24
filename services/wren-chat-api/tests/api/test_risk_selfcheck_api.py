"""API contract tests for POST /v1/risk/self-check."""

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from wren_chat_api.app import create_app
from wren_chat_api.config import Settings
from wren_chat_api.contracts import RiskSelfCheckRequest, RiskSelfCheckResponse
from wren_chat_api.errors import InvalidRiskCheckResult

_BODY = {
    "list": [
        {
            "id": "2121",
            "component": "struts2",
            "version": "2.3.31",
            "vulnerability_descriptions": [
                "Apache Struts2 远程代码执行漏洞（CVE-2017-5638），受影响版本 "
                "Struts 2.3.5 - 2.3.31、2.5 - 2.5.10。",
                "Spring Data Commons 反序列化漏洞，受影响版本 1.13 - 1.13.11。",
            ],
        },
        {
            "id": "1111",
            "component": "log4j",
            "version": "2.14.1",
            "vulnerability_descriptions": [
                "Apache Log4j2 JNDI 注入漏洞（CVE-2021-44228），受影响版本 "
                "2.0-beta9 至 2.14.1。"
            ],
        },
    ]
}


class FakeRiskSelfCheckService:
    def __init__(
        self,
        error: Exception | None = None,
        failed_ids: set[str] | None = None,
    ):
        self.error = error
        # Per-entry failure simulation (partial success); self.error, when
        # set, fails the whole batch instead.
        self.failed_ids = failed_ids or set()
        self.calls: list[RiskSelfCheckRequest] = []

    async def check_batch(self, request: RiskSelfCheckRequest) -> RiskSelfCheckResponse:
        from wren_chat_api.contracts import (
            RiskSelfCheckErrorItem,
            RiskSelfCheckResultItem,
        )

        self.calls.append(request)
        if self.error is not None:
            raise self.error
        data = []
        for item in request.items:
            if item.id in self.failed_ids:
                data.append(
                    RiskSelfCheckErrorItem(
                        id=item.id,
                        error=InvalidRiskCheckResult().public_message,
                    )
                )
            else:
                data.append(
                    RiskSelfCheckResultItem(
                        id=item.id,
                        component=item.component,
                        matched=1,
                    )
                )
        return RiskSelfCheckResponse(data=data)


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
    tmp_path, *, error: Exception | None = None, failed_ids=None, **settings_overrides
) -> tuple[FastAPI, FakeRiskSelfCheckService]:
    service = FakeRiskSelfCheckService(error, failed_ids)
    app = create_app(
        make_settings(tmp_path, **settings_overrides),
        overrides={"risk_selfcheck_service": service},
    )
    return app, service


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def post(client: httpx.AsyncClient, body: dict, *, key: str = "test-key"):
    return await client.post(
        "/v1/risk/self-check",
        headers={"Authorization": f"Bearer {key}"},
        json=body,
    )


async def test_selfcheck_success_returns_data_per_item(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await post(client, _BODY)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data"}
    assert [item["id"] for item in body["data"]] == ["2121", "1111"]
    for item in body["data"]:
        assert set(item) == {"id", "component", "matched"}
        assert item["matched"] in (0, 1)
    assert body["data"][0] == {
        "id": "2121",
        "component": "struts2",
        "matched": 1,
    }
    assert body["data"][1] == {
        "id": "1111",
        "component": "log4j",
        "matched": 1,
    }


async def test_selfcheck_failed_items_return_error_entries_still_200(tmp_path):
    app, _ = make_app(tmp_path, failed_ids={"2121"})
    async with client_for(app) as client:
        response = await post(client, _BODY)

    assert response.status_code == 200
    first, second = response.json()["data"]
    assert set(first) == {"id", "error"}
    assert first["id"] == "2121"
    assert first["error"] == InvalidRiskCheckResult().public_message
    assert second["id"] == "1111"
    assert second["matched"] == 1


async def test_selfcheck_service_receives_validated_request(tmp_path):
    app, service = make_app(tmp_path)
    async with client_for(app) as client:
        response = await post(
            client,
            {
                "list": [
                    {
                        "id": " 9 ",
                        "component": "  log4j  ",
                        "version": "2.14.1",
                        "vulnerability_descriptions": [" Apache Log4j2 JNDI 注入漏洞 "],
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "9"
    assert len(service.calls) == 1
    item = service.calls[0].items[0]
    # Whitespace is stripped before the request reaches the service, so the
    # model prompt never carries blank-padded identifiers.
    assert item.id == "9"
    assert item.component == "log4j"
    assert item.vulnerability_descriptions == ["Apache Log4j2 JNDI 注入漏洞"]


async def test_wrong_api_key_returns_401(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await post(client, _BODY, key="wrong")

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
        {},  # missing list
        {"list": []},  # list must not be empty
        {"list": [{"id": "1"}]},  # missing component/version/descriptions
        {
            "list": [
                {
                    "id": "1",
                    "component": "struts2",
                    "version": "2.3.31",
                    "vulnerability_descriptions": [],
                }
            ]
        },  # descriptions must not be empty
        {
            "list": [
                {
                    "id": "1",
                    "component": "struts2",
                    "version": "2.3.31",
                    "vulnerability_descriptions": ["   "],
                }
            ]
        },  # blank description rejected
        {
            "list": [
                {
                    "id": str(i),
                    "component": "struts2",
                    "version": "2.3.31",
                    "vulnerability_descriptions": ["x"],
                }
                for i in range(51)
            ]
        },  # over the 50-entry ceiling
        {
            "list": [
                {
                    "id": "1",
                    "component": "struts2",
                    "version": "2.3.31",
                    "vulnerability_descriptions": ["x"],
                    "extra": "field",
                }
            ]
        },  # unknown field: extra=forbid
        {"list": "not-a-list"},
    ],
)
async def test_invalid_bodies_return_400(tmp_path, body):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await post(client, body)

    assert response.status_code == 400
    error_body = response.json()["error"]
    assert error_body["code"] == "INVALID_REQUEST"
    assert "list" in error_body["message"]


@pytest.mark.parametrize(
    "error,expected_status,expected_code",
    [
        (InvalidRiskCheckResult(), 502, "INVALID_RISK_CHECK_RESULT"),
    ],
)
async def test_service_errors_map_to_stable_http_responses(
    tmp_path, error, expected_status, expected_code
):
    # Whole-batch failures (raised by check_batch itself) still map to the
    # stable envelope; per-entry failures never reach this path.
    app, _ = make_app(tmp_path, error=error)
    async with client_for(app) as client:
        response = await post(client, _BODY)

    assert response.status_code == expected_status
    assert response.json() == {
        "error": {"code": expected_code, "message": error.public_message}
    }
