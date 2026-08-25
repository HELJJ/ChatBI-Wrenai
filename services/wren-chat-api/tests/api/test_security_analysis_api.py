"""API contract tests for POST /v1/security-report/analysis."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from wren_chat_api.app import create_app
from wren_chat_api.config import Settings
from wren_chat_api.contracts import SecurityAnalysisResponse
from wren_chat_api.errors import (
    InvalidAnalysisResult,
    InvalidReportFile,
    ReportTooLarge,
    RequestTimedOut,
    UpstreamFailed,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

_SAMPLE_ANALYSIS = SecurityAnalysisResponse(
    filename="etc_detect_report_sample.md",
    server_info={
        "hostname": "localhost.localdomain",
        "os": "Kylin Linux Advanced Server V10 (Lance)",
        "kernel": "4.19.90-52.22.v2207.ky10.x86_64",
    },
    modules=[
        {
            "module": "身份鉴别",
            "total": 2,
            "passed": 1,
            "failed": 1,
            "check_items": [
                {
                    "check_item": "密码有效期",
                    "passed": False,
                    "severity": "高危",
                    "current_status": "当前为 99999",
                    "risk_description": "密码永不过期，不符合等保 2.0 口令定期更换要求。",
                    "recommendation": "编辑 /etc/login.defs 将 PASS_MAX_DAYS 设为 90。",
                },
                {
                    "check_item": "密码复杂度",
                    "passed": True,
                    "severity": "中危",
                    "current_status": "已配置 minlen=8",
                    "risk_description": "已启用口令复杂度策略，满足等保 2.0 要求。",
                    "recommendation": "保持现有配置并定期复查。",
                },
            ],
        }
    ],
    summary="共发现多项不合规，建议优先整改口令与审计类问题。",
)


class FakeAnalysisService:
    def __init__(
        self,
        error: Exception | None = None,
        result: SecurityAnalysisResponse | None = None,
    ):
        self.error = error
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def analyze(self, filename: str, content: str) -> SecurityAnalysisResponse:
        self.calls.append((filename, content))
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result.model_copy(update={"filename": filename})
        return _SAMPLE_ANALYSIS.model_copy(update={"filename": filename})


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
    tmp_path, *, error: Exception | None = None, **settings_overrides
) -> tuple[FastAPI, FakeAnalysisService]:
    service = FakeAnalysisService(error)
    app = create_app(
        make_settings(tmp_path, **settings_overrides),
        overrides={"analysis_service": service},
    )
    return app, service


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def upload(report_bytes: bytes, filename: str = "report.md") -> dict:
    return {"file": (filename, report_bytes, "text/markdown")}


@pytest.fixture
def sample_report() -> bytes:
    return (FIXTURES / "etc_detect_report_sample.md").read_bytes()


async def test_analysis_success_returns_structured_result(tmp_path, sample_report):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/security-report/analysis",
            headers={"Authorization": "Bearer test-key"},
            files=upload(sample_report, "etc_detect_report_sample.md"),
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "filename",
        "server_info",
        "modules",
        "summary",
        "partial",
    }
    assert body["partial"] is False
    assert body["filename"] == "etc_detect_report_sample.md"
    assert body["server_info"]["os"] == "Kylin Linux Advanced Server V10 (Lance)"
    assert len(body["modules"]) == 1
    module = body["modules"][0]
    assert module["module"] == "身份鉴别"
    assert (module["total"], module["passed"], module["failed"]) == (2, 1, 1)
    assert module["check_items"][0]["check_item"] == "密码有效期"
    assert module["check_items"][0]["passed"] is False
    assert "PASS_MAX_DAYS" in module["check_items"][0]["recommendation"]
    assert module["check_items"][1]["check_item"] == "密码复杂度"
    assert module["check_items"][1]["passed"] is True


async def test_analysis_service_receives_validated_content(tmp_path, sample_report):
    app, service = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/security-report/analysis",
            headers={"Authorization": "Bearer test-key"},
            files=upload(sample_report, "report.md"),
        )

    assert response.status_code == 200
    assert len(service.calls) == 1
    filename, content = service.calls[0]
    assert filename == "report.md"
    assert "等保2.0 服务器安全检查报告" in content


async def test_missing_summary_is_omitted_not_placeholder(tmp_path, sample_report):
    # A salvaged partial result whose summary was never generated must omit
    # the field entirely (exclude_none), never show placeholder text.
    service = FakeAnalysisService(
        result=_SAMPLE_ANALYSIS.model_copy(update={"summary": None, "partial": True})
    )
    app = create_app(make_settings(tmp_path), overrides={"analysis_service": service})
    async with client_for(app) as client:
        response = await client.post(
            "/v1/security-report/analysis",
            headers={"Authorization": "Bearer test-key"},
            files=upload(sample_report),
        )

    assert response.status_code == 200
    body = response.json()
    assert "summary" not in body
    assert body["partial"] is True
    assert "截断" not in response.text


async def test_wrong_api_key_returns_401(tmp_path, sample_report):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/security-report/analysis",
            headers={"Authorization": "Bearer wrong"},
            files=upload(sample_report),
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"


async def test_missing_authorization_returns_401(tmp_path, sample_report):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/security-report/analysis",
            files=upload(sample_report),
        )

    assert response.status_code == 401


async def test_non_markdown_extension_returns_422(tmp_path, sample_report):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/security-report/analysis",
            headers={"Authorization": "Bearer test-key"},
            files=upload(sample_report, "report.txt"),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REPORT_FILE"


async def test_oversized_report_returns_413(tmp_path):
    app, _ = make_app(tmp_path, max_report_bytes=1024)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/security-report/analysis",
            headers={"Authorization": "Bearer test-key"},
            files=upload(b"x" * 1025),
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REPORT_TOO_LARGE"


async def test_missing_file_field_returns_400(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/security-report/analysis",
            headers={"Authorization": "Bearer test-key"},
            data={},
        )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert "multipart/form-data" in body["error"]["message"]


@pytest.mark.parametrize(
    "error,expected_status,expected_code",
    [
        (InvalidReportFile(), 422, "INVALID_REPORT_FILE"),
        (ReportTooLarge(), 413, "REPORT_TOO_LARGE"),
        (UpstreamFailed(), 502, "UPSTREAM_FAILED"),
        (InvalidAnalysisResult(), 502, "INVALID_ANALYSIS_RESULT"),
        (RequestTimedOut(), 504, "REQUEST_TIMED_OUT"),
    ],
)
async def test_service_errors_map_to_stable_http_responses(
    tmp_path, sample_report, error, expected_status, expected_code
):
    app, _ = make_app(tmp_path, error=error)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/security-report/analysis",
            headers={"Authorization": "Bearer test-key"},
            files=upload(sample_report),
        )

    assert response.status_code == expected_status
    assert response.json() == {
        "error": {"code": expected_code, "message": error.public_message}
    }
