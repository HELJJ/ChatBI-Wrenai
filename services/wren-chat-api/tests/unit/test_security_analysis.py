"""Unit contracts for the security-report analysis module."""

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from openai import APIConnectionError
from pydantic import ValidationError

from wren_chat_api.config import Settings
from wren_chat_api.contracts import SecurityAnalysis
from wren_chat_api.errors import (
    InvalidAnalysisResult,
    InvalidReportFile,
    ReportTooLarge,
    RequestTimedOut,
    UpstreamFailed,
)
from wren_chat_api.security_analysis import (
    AnalysisService,
    _extract_json,
    validate_report,
)

_VALID_CONTENT = "# 等保2.0 服务器安全检查报告\n\n- 密码有效期：FAIL\n"

_VALID_ANALYSIS_JSON = {
    "server_info": {
        "hostname": "localhost.localdomain",
        "os": "Kylin Linux Advanced Server V10",
        "kernel": "4.19.90-52.22.v2207.ky10.x86_64",
    },
    "risk_level": "high",
    "risk_items": [
        {
            "check_item": "密码有效期",
            "severity": "high",
            "current_status": "当前为 99999",
            "risk_description": "密码长期不更换，不符合等保要求",
            "recommendation": "将 PASS_MAX_DAYS 设为 90",
        }
    ],
    "summary": "存在多项不合规，建议优先整改口令策略。",
}


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "state_database_url": "postgresql://user:pass@localhost:5432/wren_test",
        "api_key": "test-key",
        "project_path": tmp_path,
        "model": "test-model",
    }
    values.update(overrides)
    return Settings(**values, _env_file=None)


class FakeModel:
    def __init__(self, *, content: str = "", delay: float = 0, error=None):
        self.content = content
        self.delay = delay
        self.error = error
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.content)


# --- validate_report -------------------------------------------------------


def test_validate_report_accepts_markdown_and_decodes_utf8():
    content = validate_report("report.md", _VALID_CONTENT.encode("utf-8"), 1024)

    assert content == _VALID_CONTENT


@pytest.mark.parametrize("filename", ["report.MD", "report.markdown", "a/b.MARKDOWN"])
def test_validate_report_accepts_suffix_case_insensitively(filename):
    content = validate_report(filename, _VALID_CONTENT.encode("utf-8"), 1024)

    assert content == _VALID_CONTENT


@pytest.mark.parametrize("filename", [None, "report.txt", "report", "report.md.exe"])
def test_validate_report_rejects_non_markdown_filenames(filename):
    with pytest.raises(InvalidReportFile):
        validate_report(filename, _VALID_CONTENT.encode("utf-8"), 1024)


def test_validate_report_rejects_oversized_upload():
    raw = b"x" * 1025

    with pytest.raises(ReportTooLarge):
        validate_report("report.md", raw, 1024)


def test_validate_report_boundary_size_is_allowed():
    raw = b"x" * 1024

    assert validate_report("report.md", raw, 1024) == "x" * 1024


def test_validate_report_rejects_non_utf8_bytes():
    with pytest.raises(InvalidReportFile):
        validate_report("report.md", b"\xff\xfe\x00\x01", 1024)


def test_validate_report_rejects_blank_content():
    with pytest.raises(InvalidReportFile):
        validate_report("report.md", b"   \n\t ", 1024)


# --- _extract_json ---------------------------------------------------------


def test_extract_json_parses_bare_json():
    payload = json.dumps(_VALID_ANALYSIS_JSON, ensure_ascii=False)

    assert _extract_json(payload) == _VALID_ANALYSIS_JSON


def test_extract_json_strips_code_fences_and_surrounding_text():
    text = (
        "以下是分析结果：\n"
        "```json\n" + json.dumps(_VALID_ANALYSIS_JSON, ensure_ascii=False) + "\n```\n"
        "希望对你有帮助。"
    )

    assert _extract_json(text) == _VALID_ANALYSIS_JSON


def test_extract_json_raises_without_object():
    with pytest.raises(ValueError):
        _extract_json("no json here at all")


def test_extract_json_raises_on_malformed_json():
    with pytest.raises(ValueError):
        _extract_json('{"risk_level": ')


# --- AnalysisService -------------------------------------------------------


def make_service(tmp_path, model) -> AnalysisService:
    return AnalysisService(model=model, settings=make_settings(tmp_path))


async def test_analyze_returns_structured_response(tmp_path):
    model = FakeModel(content=json.dumps(_VALID_ANALYSIS_JSON, ensure_ascii=False))
    service = make_service(tmp_path, model)

    response = await service.analyze(r"C:\upload\etc_detect_report.md", _VALID_CONTENT)

    assert response.filename == "etc_detect_report.md"
    assert response.risk_level == "high"
    assert response.server_info.hostname == "localhost.localdomain"
    assert len(response.risk_items) == 1
    assert response.risk_items[0].check_item == "密码有效期"
    assert response.risk_items[0].recommendation == "将 PASS_MAX_DAYS 设为 90"
    assert response.summary == "存在多项不合规，建议优先整改口令策略。"


async def test_analyze_sends_system_prompt_and_report(tmp_path):
    model = FakeModel(content=json.dumps(_VALID_ANALYSIS_JSON, ensure_ascii=False))
    service = make_service(tmp_path, model)

    await service.analyze("report.md", _VALID_CONTENT)

    assert len(model.calls) == 1
    messages = model.calls[0]
    assert isinstance(messages[0], SystemMessage)
    assert "等保" in messages[0].content
    assert isinstance(messages[-1], HumanMessage)
    assert messages[-1].content == _VALID_CONTENT


async def test_analyze_allows_report_without_risk_items(tmp_path):
    payload = {
        "server_info": {"hostname": None, "os": None, "kernel": None},
        "risk_level": "low",
        "risk_items": [],
        "summary": "全部合规。",
    }
    model = FakeModel(content=json.dumps(payload, ensure_ascii=False))
    service = make_service(tmp_path, model)

    response = await service.analyze("report.md", _VALID_CONTENT)

    assert response.risk_items == []
    assert response.risk_level == "low"


async def test_analyze_timeout_maps_to_request_timed_out(tmp_path):
    model = FakeModel(delay=10)
    settings = make_settings(tmp_path, analysis_timeout_seconds=1)
    service = AnalysisService(model=model, settings=settings)

    with pytest.raises(RequestTimedOut):
        await service.analyze("report.md", _VALID_CONTENT)


async def test_analyze_upstream_error_maps_to_upstream_failed(tmp_path):
    model = FakeModel(
        error=APIConnectionError(
            request=httpx.Request("POST", "https://maas.example/v1/chat/completions")
        )
    )
    service = make_service(tmp_path, model)

    with pytest.raises(UpstreamFailed):
        await service.analyze("report.md", _VALID_CONTENT)


async def test_analyze_non_json_output_maps_to_invalid_analysis_result(tmp_path):
    model = FakeModel(content="抱歉，我无法分析该报告。")
    service = make_service(tmp_path, model)

    with pytest.raises(InvalidAnalysisResult):
        await service.analyze("report.md", _VALID_CONTENT)


async def test_analyze_schema_violation_maps_to_invalid_analysis_result(tmp_path):
    payload = dict(_VALID_ANALYSIS_JSON)
    payload["risk_level"] = "extreme"
    model = FakeModel(content=json.dumps(payload, ensure_ascii=False))
    service = make_service(tmp_path, model)

    with pytest.raises(InvalidAnalysisResult):
        await service.analyze("report.md", _VALID_CONTENT)


def test_analysis_rejects_unknown_fields():
    payload = dict(_VALID_ANALYSIS_JSON)
    payload["unexpected"] = True

    with pytest.raises(ValidationError):
        SecurityAnalysis.model_validate(payload)


def test_analysis_rejects_invalid_risk_level():
    payload = dict(_VALID_ANALYSIS_JSON)
    payload["risk_level"] = "extreme"

    with pytest.raises(ValidationError):
        SecurityAnalysis.model_validate(payload)
