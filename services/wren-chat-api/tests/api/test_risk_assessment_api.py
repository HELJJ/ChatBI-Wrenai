"""API contract tests for POST /v1/risk-assessment/extract."""

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from wren_chat_api.app import create_app
from wren_chat_api.config import Settings
from wren_chat_api.contracts import RiskAssessmentExtractResponse
from wren_chat_api.errors import (
    InvalidRiskAssessmentResult,
    InvalidRiskFile,
    RequestTimedOut,
    RiskDocConversionFailed,
    UpstreamFailed,
)

_SAMPLE_EXTRACTION = RiskAssessmentExtractResponse(
    filename="报告.docx",
    riskHigh=1,
    riskHighRate=0.02,
    riskMedium=5,
    riskMediumRate=0.09,
    riskLow=46,
    riskLowRate=0.87,
    finalEvaluationCode="L",
    finalEvaluationName="低风险",
)

_DOCX_BYTES = b"PK\x03\x04%some zip payload"
_DOC_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1some ole payload"


class FakeRiskAssessmentService:
    def __init__(
        self,
        error: Exception | None = None,
        result: RiskAssessmentExtractResponse | None = None,
    ):
        self.error = error
        self.result = result
        self.calls: list[tuple[str, bytes]] = []

    async def extract(self, filename: str, raw: bytes) -> RiskAssessmentExtractResponse:
        self.calls.append((filename, raw))
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result.model_copy(update={"filename": filename})
        return _SAMPLE_EXTRACTION.model_copy(update={"filename": filename})


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
) -> tuple[FastAPI, FakeRiskAssessmentService]:
    service = FakeRiskAssessmentService(error)
    app = create_app(
        make_settings(tmp_path, **settings_overrides),
        overrides={"risk_assessment_service": service},
    )
    return app, service


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def upload(doc_bytes: bytes, filename: str = "报告.docx") -> dict:
    return {"file": (filename, doc_bytes, "application/octet-stream")}


async def test_extraction_success_returns_structured_result(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract",
            headers={"Authorization": "Bearer test-key"},
            files=upload(_DOCX_BYTES),
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "filename",
        "riskHigh",
        "riskHighRate",
        "riskMedium",
        "riskMediumRate",
        "riskLow",
        "riskLowRate",
        "finalEvaluationCode",
        "finalEvaluationName",
    }
    assert body["filename"] == "报告.docx"
    assert body["riskHigh"] == 1
    assert body["riskHighRate"] == 0.02
    assert body["riskMedium"] == 5
    assert body["riskMediumRate"] == 0.09
    assert body["riskLow"] == 46
    assert body["riskLowRate"] == 0.87
    assert body["finalEvaluationCode"] == "L"
    assert body["finalEvaluationName"] == "低风险"


async def test_service_receives_filename_and_bytes(tmp_path):
    app, service = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract",
            headers={"Authorization": "Bearer test-key"},
            files=upload(_DOC_BYTES, "报告.doc"),
        )

    assert response.status_code == 200
    assert len(service.calls) == 1
    filename, raw = service.calls[0]
    assert filename == "报告.doc"
    assert raw == _DOC_BYTES


async def test_wrong_api_key_returns_401(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract",
            headers={"Authorization": "Bearer wrong"},
            files=upload(_DOCX_BYTES),
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"


async def test_missing_authorization_returns_401(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract",
            files=upload(_DOCX_BYTES),
        )

    assert response.status_code == 401


async def test_non_doc_suffix_returns_422(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract",
            headers={"Authorization": "Bearer test-key"},
            files=upload(_DOCX_BYTES, "报告.txt"),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RISK_FILE"


async def test_bytes_without_zip_or_ole_magic_return_422(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract",
            headers={"Authorization": "Bearer test-key"},
            files=upload(b"plain text pretending to be a report"),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RISK_FILE"


async def test_missing_file_field_returns_400(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract",
            headers={"Authorization": "Bearer test-key"},
            data={},
        )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert ".doc/.docx" in body["error"]["message"]


@pytest.mark.parametrize(
    "error,expected_status,expected_code",
    [
        (InvalidRiskFile(), 422, "INVALID_RISK_FILE"),
        (RiskDocConversionFailed(), 502, "RISK_DOC_CONVERSION_FAILED"),
        (
            InvalidRiskAssessmentResult(),
            502,
            "INVALID_RISK_ASSESSMENT_RESULT",
        ),
        (UpstreamFailed(), 502, "UPSTREAM_FAILED"),
        (RequestTimedOut(), 504, "REQUEST_TIMED_OUT"),
    ],
)
async def test_service_errors_map_to_stable_http_responses(
    tmp_path, error, expected_status, expected_code
):
    app, _ = make_app(tmp_path, error=error)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract",
            headers={"Authorization": "Bearer test-key"},
            files=upload(_DOCX_BYTES),
        )

    assert response.status_code == expected_status
    assert response.json() == {
        "error": {"code": expected_code, "message": error.public_message}
    }
