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
    _salvage_analysis,
    validate_report,
)

_VALID_CONTENT = "# 等保2.0 服务器安全检查报告\n\n- 密码有效期：FAIL\n"

_VALID_ANALYSIS_JSON = {
    "server_info": {
        "hostname": "localhost.localdomain",
        "os": "Kylin Linux Advanced Server V10",
        "kernel": "4.19.90-52.22.v2207.ky10.x86_64",
    },
    "modules": [
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
                    "risk_description": "密码长期不更换，不符合等保要求",
                    "recommendation": "将 PASS_MAX_DAYS 设为 90",
                },
                {
                    "check_item": "密码复杂度",
                    "passed": True,
                    "severity": "中危",
                    "current_status": "已配置 minlen=8",
                    "risk_description": "已启用口令复杂度策略，满足等保要求",
                    "recommendation": "保持现有配置，定期复查",
                },
            ],
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
    def __init__(
        self,
        *,
        content: str = "",
        delay: float = 0,
        error=None,
        rounds: list[tuple[str, str]] | None = None,
    ):
        self.delay = delay
        self.error = error
        # One (content, finish_reason) pair per ainvoke call; the final
        # pair repeats for any further calls.
        self.rounds = rounds or [(content, "stop")]
        self.calls = []
        self.bound_kwargs: dict = {}

    def bind(self, **kwargs):
        self.bound_kwargs = kwargs
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        index = min(len(self.calls) - 1, len(self.rounds) - 1)
        content, finish_reason = self.rounds[index]
        return AIMessage(
            content=content,
            response_metadata={"finish_reason": finish_reason},
        )


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
        _extract_json('{"modules": ')


# --- AnalysisService -------------------------------------------------------


def make_service(tmp_path, model) -> AnalysisService:
    return AnalysisService(model=model, settings=make_settings(tmp_path))


async def test_analyze_returns_structured_response(tmp_path):
    model = FakeModel(content=json.dumps(_VALID_ANALYSIS_JSON, ensure_ascii=False))
    service = make_service(tmp_path, model)

    response = await service.analyze(r"C:\upload\etc_detect_report.md", _VALID_CONTENT)

    assert response.filename == "etc_detect_report.md"
    assert response.server_info.hostname == "localhost.localdomain"
    assert len(response.modules) == 1
    module = response.modules[0]
    assert module.module == "身份鉴别"
    assert (module.total, module.passed, module.failed) == (2, 1, 1)
    assert module.check_items[0].check_item == "密码有效期"
    assert module.check_items[0].passed is False
    assert module.check_items[0].recommendation == "将 PASS_MAX_DAYS 设为 90"
    assert module.check_items[1].check_item == "密码复杂度"
    assert module.check_items[1].passed is True
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


async def test_analyze_allows_report_without_modules(tmp_path):
    payload = {
        "server_info": {"hostname": None, "os": None, "kernel": None},
        "modules": [],
        "summary": "全部合规。",
    }
    model = FakeModel(content=json.dumps(payload, ensure_ascii=False))
    service = make_service(tmp_path, model)

    response = await service.analyze("report.md", _VALID_CONTENT)

    assert response.modules == []


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


# A field-accurate truncation: the first check item is complete, generation
# cuts mid-string inside the second one.
_TRUNCATED = (
    '{"server_info": {"hostname": "localhost.localdomain", "os": "Kylin", '
    '"kernel": "4.19.90"}, '
    '"modules": ['
    '{"module": "身份鉴别", "total": 2, "passed": 1, "failed": 1, "check_items": ['
    '{"check_item": "密码有效期", "passed": false, "severity": "高危", '
    '"current_status": "99999", "risk_description": "密码不更换", '
    '"recommendation": "PASS_MAX_DAYS=90"}, '
    '{"check_item": "密码复杂度", "passed": false, "severity": "严重'
)


async def test_analyze_continues_truncated_output_until_complete(tmp_path):
    complete = json.dumps(_VALID_ANALYSIS_JSON, ensure_ascii=False)
    head = complete[: int(len(complete) * 0.55)]
    tail = complete[len(head) :]
    model = FakeModel(rounds=[(head, "length"), (tail, "stop")])
    service = make_service(tmp_path, model)

    response = await service.analyze("report.md", _VALID_CONTENT)

    assert response.partial is False
    assert len(response.modules) == 1
    assert len(model.calls) == 2
    continued_messages = model.calls[1]
    assert continued_messages[-2].content == head
    assert "继续输出剩余" in continued_messages[-1].content


async def test_analyze_salvages_when_continuations_exhausted(tmp_path):
    model = FakeModel(
        rounds=[
            (_TRUNCATED, "length"),
            ("仍然被截断的续写片段", "length"),
            ("还是没有结束", "length"),
            ("第四次依旧截断", "length"),
        ]
    )
    service = make_service(tmp_path, model)

    response = await service.analyze("report.md", _VALID_CONTENT)

    # 1 initial call + 3 continuation rounds (the configured cap).
    assert len(model.calls) == 4
    assert response.partial is True
    module = response.modules[0]
    assert [item.check_item for item in module.check_items] == ["密码有效期"]
    # Counts truncated alongside the cut item are re-derived from what
    # actually survived, not kept from the half-written figure.
    assert (module.total, module.passed, module.failed) == (1, 0, 1)
    assert response.summary is None  # no placeholder text fabricated


async def test_analyze_salvages_immediately_when_continuations_disabled(
    tmp_path,
):
    model = FakeModel(rounds=[(_TRUNCATED, "length")])
    settings = make_settings(tmp_path, analysis_max_continuations=0)
    service = AnalysisService(model=model, settings=settings)

    response = await service.analyze("report.md", _VALID_CONTENT)

    assert len(model.calls) == 1
    assert response.partial is True
    assert len(response.modules) == 1


async def test_analyze_salvages_truncated_json_on_normal_stop(tmp_path):
    # Complete-looking call whose JSON is cut mid-string still returns a
    # salvaged partial result instead of failing the request.
    model = FakeModel(content=_TRUNCATED)
    service = make_service(tmp_path, model)

    response = await service.analyze("report.md", _VALID_CONTENT)

    assert response.partial is True
    assert response.modules[0].check_items[0].check_item == "密码有效期"


async def test_analyze_unsalvageable_truncation_maps_to_invalid(tmp_path):
    model = FakeModel(rounds=[('{"modul', "length")])
    settings = make_settings(tmp_path, analysis_max_continuations=0)
    service = AnalysisService(model=model, settings=settings)

    with pytest.raises(InvalidAnalysisResult):
        await service.analyze("report.md", _VALID_CONTENT)


async def test_analyze_non_json_output_maps_to_invalid_analysis_result(tmp_path):
    model = FakeModel(content="抱歉，我无法分析该报告。")
    service = make_service(tmp_path, model)

    with pytest.raises(InvalidAnalysisResult):
        await service.analyze("report.md", _VALID_CONTENT)


async def test_analyze_repairs_module_counts_via_salvage(tmp_path):
    # A module with unusable counts fails strict validation and is repaired
    # from its check items as a partial result instead of failing.
    payload = json.loads(json.dumps(_VALID_ANALYSIS_JSON, ensure_ascii=False))
    payload["modules"][0]["total"] = "很多"
    model = FakeModel(content=json.dumps(payload, ensure_ascii=False))
    service = make_service(tmp_path, model)

    response = await service.analyze("report.md", _VALID_CONTENT)

    assert response.partial is True
    module = response.modules[0]
    assert (module.total, module.passed, module.failed) == (2, 1, 1)


# --- _salvage_analysis -----------------------------------------------------


def test_salvage_rederives_module_counts_from_items():
    truncated = (
        '{"server_info": {"os": "Kylin V10"}, "modules": ['
        '{"module": "安全审计", "total": 5, "passed": 3, "failed": 2, '
        '"check_items": ['
        '{"check_item": "密码有效期", "passed": false, "severity": "严重", '
        '"current_status": "99999", "risk_description": "x", '
        '"recommendation": "y"}'
    )

    salvaged = _salvage_analysis(truncated)

    module = salvaged.modules[0]
    assert (module.total, module.passed, module.failed) == (1, 0, 1)
    assert module.check_items[0].passed is False
    assert salvaged.summary is None


def test_salvage_backfills_missing_passed_as_false():
    # Truncation can cut an item before its "passed" flag was generated;
    # the unknown outcome defaults to not passed so the item does not
    # silently look compliant.
    truncated = (
        '{"modules": [{"module": "身份鉴别", "check_items": ['
        '{"check_item": "密码有效期", "severity": "高危", '
        '"current_status": "99999", "risk_description": "x", '
        '"recommendation": "y"}'
    )

    salvaged = _salvage_analysis(truncated)

    assert salvaged.modules[0].check_items[0].passed is False


def test_salvage_names_unnamed_module_with_items():
    # A module whose name was cut off keeps its items under a fallback
    # name; a nameless, itemless fragment is dropped entirely.
    truncated = (
        '{"modules": [{"check_items": ['
        '{"check_item": "a", "passed": true, "severity": "提示", '
        '"current_status": "s", "risk_description": "x", '
        '"recommendation": "y"}'
    )

    salvaged = _salvage_analysis(truncated)

    assert salvaged.modules[0].module == "未命名模块"
    assert salvaged.modules[0].total == 1


def test_salvage_skips_brace_inside_string_value():
    # The last "}" before the cut sits inside a recommendation string; the
    # repair must fall back to the previous real closing brace.
    truncated = (
        '{"server_info": {"os": "x"}, "modules": ['
        '{"module": "身份鉴别", "check_items": ['
        '{"check_item": "a", "severity": "低危", "current_status": "s", '
        '"risk_description": "d", "recommendation": "run } cmd'
    )

    salvaged = _salvage_analysis(truncated)

    assert salvaged.server_info.os == "x"
    assert salvaged.modules == []


def test_salvage_raises_without_complete_fragment():
    with pytest.raises(ValueError):
        _salvage_analysis('{"modules": "hi')


def test_analysis_rejects_unknown_fields():
    payload = dict(_VALID_ANALYSIS_JSON)
    payload["unexpected"] = True

    with pytest.raises(ValidationError):
        SecurityAnalysis.model_validate(payload)


def test_analysis_rejects_module_without_name():
    payload = json.loads(json.dumps(_VALID_ANALYSIS_JSON, ensure_ascii=False))
    del payload["modules"][0]["module"]

    with pytest.raises(ValidationError):
        SecurityAnalysis.model_validate(payload)


def test_analysis_rejects_check_item_without_passed():
    payload = json.loads(json.dumps(_VALID_ANALYSIS_JSON, ensure_ascii=False))
    payload["modules"][0]["check_items"] = [
        {key: value for key, value in item.items() if key != "passed"}
        for item in payload["modules"][0]["check_items"]
    ]

    with pytest.raises(ValidationError):
        SecurityAnalysis.model_validate(payload)
