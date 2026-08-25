"""LLM-backed security analysis of uploaded server check reports."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from openai import APIError as OpenAIAPIError
from pydantic import ValidationError

from wren_chat_api.config import Settings
from wren_chat_api.contracts import SecurityAnalysis, SecurityAnalysisResponse
from wren_chat_api.errors import (
    InvalidAnalysisResult,
    InvalidReportFile,
    ReportTooLarge,
    RequestTimedOut,
    UpstreamFailed,
)
from wren_chat_api.metrics import ANALYSIS_TRUNCATIONS

logger = logging.getLogger(__name__)

_ALLOWED_SUFFIXES = frozenset({".md", ".markdown"})

# Structured output is requested via the prompt and parsed locally instead of
# with_structured_output(): every OpenAI-compatible endpoint reliably returns
# plain text, while function-calling support varies across MaaS providers.
_SYSTEM_PROMPT = """\
你是一名资深的 Linux 服务器安全专家，熟悉 GB/T 22239-2019\
（网络安全等级保护基本要求，等保 2.0）。

用户会提供一份 Markdown 格式的服务器安全检查报告，报告中包含多个\
检查模块，每个模块下有多个检查项。请按模块分析该报告并输出风险评估\
结果，要求：

1. server_info：提取报告中的主机名（hostname）、操作系统（os）、\
内核版本（kernel）；报告中没有的字段填 null。
2. modules：按报告中的模块结构逐模块输出（模块名称、顺序与报告一致），\
每个模块包含 module（模块名称）、total（总项数）、passed（通过数）、\
failed（不通过数，均取自报告的汇总表）和 check_items\
（该模块的所有检查项，含通过项）：
   - 每个模块中，报告里的每个 FAIL 项和 PASS 项都必须列出，\
未通过项在前、按严重度从高到低排序，通过项在后；
   - INFO 项，以及报告未覆盖但结合系统信息（操作系统、内核版本等）\
值得警惕的问题，也应列入所属模块（视为未通过）；\
不属于任何已有模块的，归入名为"其他"的模块；
   - 每个检查项包含：check_item（检查项名称）、passed（是否通过：\
PASS 为 true；FAIL、INFO 及报告未覆盖的项为 false）、severity\
（严重度，取 "严重"、"高危"、"中危"、"低危"、"提示" 之一，\
按该项不合规时的风险程度评定）、current_status（当前状态，\
引用报告中的原始数值）、risk_description（风险说明：未通过项说明\
不整改可能带来的后果；通过项简要说明当前已满足的要求）、\
recommendation（整改建议：未通过项尽量给出具体的配置文件路径或\
命令；通过项给出保持建议）。
3. summary：用中文简要总结整体安全状况，并给出整改优先级建议。

输出必须完整，严格控制篇幅以防被截断：每项的 risk_description\
与 recommendation 各不超过 80 字（通过项各不超过 40 字）；\
summary 不超过 120 字。

严格只输出一个 JSON 对象：不要使用 Markdown 代码围栏，\
不要输出任何解释性文字。JSON 结构如下：
{"server_info": {"hostname": 字符串或null, "os": 字符串或null, \
"kernel": 字符串或null},\
 "modules": [{"module": 字符串, "total": 整数, "passed": 整数, \
"failed": 整数, "check_items": [{"check_item": 字符串, \
"passed": true|false, "severity": "严重"|"高危"|"中危"|"低危"|"提示", \
"current_status": 字符串, "risk_description": 字符串, \
"recommendation": 字符串}]}],\
 "summary": 字符串}

所有字符串内容使用中文（server_info 保留报告原文）。"""

_CONTINUATION_PROMPT = (
    "你的输出因达到 token 上限被截断。请从中断处的下一个字符继续输出剩余的"
    "JSON：不要重复已输出的任何内容，不要添加任何解释，"
    "直到整个 JSON 对象完整结束。"
)

# Salvage fallbacks: when every continuation round still ends at the token
# limit, return the fully generated modules instead of failing the request
# (field-observed gateways cut generation mid-string). Missing fields are
# derived or omitted — placeholder text is never fabricated.


def validate_report(filename: str | None, raw: bytes, max_bytes: int) -> str:
    """Validate one uploaded report and return its decoded markdown content."""
    if filename is None or Path(filename).suffix.lower() not in _ALLOWED_SUFFIXES:
        raise InvalidReportFile("filename")
    if len(raw) > max_bytes:
        raise ReportTooLarge(f"{len(raw)} bytes exceeds {max_bytes}")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidReportFile("utf-8 decode", cause=exc) from exc
    if not content.strip():
        raise InvalidReportFile("empty content")
    return content


def _display_filename(filename: str) -> str:
    """Reduce an upload filename to its basename before echoing it back."""
    return filename.replace("\\", "/").rsplit("/", 1)[-1]


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the JSON object in a model response, tolerating code fences."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object found in model response")
    return json.loads(text[start : end + 1])


def _missing_closes(text: str) -> str:
    """Closing brackets needed to terminate truncated JSON, string-aware."""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    return "".join("]" if ch == "[" else "}" for ch in reversed(stack))


def _salvage_module(module: dict[str, Any]) -> dict[str, Any] | None:
    """Repair one salvaged module, or None when nothing usable remains."""
    items = [
        item for item in (module.get("check_items") or []) if isinstance(item, dict)
    ]
    # A "passed" flag cut off by truncation defaults to false: an unknown
    # outcome must not silently look compliant.
    for item in items:
        item.setdefault("passed", False)
    # Counts cut off by truncation are re-derived from the recovered items;
    # self-consistent numbers beat a half-written summary-table figure.
    module["check_items"] = items
    module["total"] = len(items)
    module["passed"] = sum(1 for item in items if item.get("passed"))
    module["failed"] = len(items) - module["passed"]
    if not isinstance(module.get("module"), str) or not module["module"].strip():
        return None if not items else {**module, "module": "未命名模块"}
    return module


def _salvage_analysis(text: str) -> SecurityAnalysis:
    """Recover a partial analysis from truncated JSON, dropping the
    incomplete trailing check item and back-filling missing fields."""
    pos = text.rfind("}")
    while pos != -1:
        candidate = text[: pos + 1]
        try:
            data = json.loads(candidate + _missing_closes(candidate))
        except ValueError:
            # The last "}" sat inside a string value; try the previous one.
            pos = text.rfind("}", 0, pos)
            continue
        break
    else:
        raise ValueError("no complete JSON fragment to salvage")
    if not isinstance(data, dict):
        raise ValueError("salvaged JSON fragment is not an object")

    modules = [
        repaired
        for module in (data.get("modules") or [])
        if isinstance(module, dict) and (repaired := _salvage_module(module))
    ]
    data["modules"] = modules
    data.setdefault("server_info", {})
    return SecurityAnalysis.model_validate(data)


class AnalysisService:
    """Analyze one uploaded server report with the configured LLM.

    Truncation handling (finish_reason=length): each round appends the
    partial output as an assistant message and asks the model to continue;
    once continuation rounds are exhausted, the partial output is salvaged
    into a response marked ``partial`` instead of failing the request.
    """

    def __init__(self, *, model: Any, settings: Settings) -> None:
        # Explicit output ceiling: some OpenAI-compatible gateways silently
        # truncate at their own default (observed: 5120 tokens) when the
        # request omits max_tokens, cutting the JSON mid-string.
        self._model = model.bind(max_tokens=settings.analysis_max_tokens)
        self._settings = settings

    async def analyze(self, filename: str, content: str) -> SecurityAnalysisResponse:
        """Run the LLM analysis of one report, or raise a typed error."""
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=content),
        ]
        parts: list[str] = []
        rounds = 0
        while True:
            response = await self._invoke(messages)
            parts.append(str(response.content))
            finish_reason = response.response_metadata.get("finish_reason")
            if (
                finish_reason != "length"
                or rounds >= self._settings.analysis_max_continuations
            ):
                break
            rounds += 1
            ANALYSIS_TRUNCATIONS.labels(outcome="continued").inc()
            messages = [
                *messages,
                AIMessage(content="".join(parts)),
                HumanMessage(content=_CONTINUATION_PROMPT),
            ]

        text = "".join(parts)
        partial = False
        try:
            analysis = SecurityAnalysis.model_validate(_extract_json(text))
        except (ValueError, ValidationError):
            logger.warning(
                "security analysis output failed validation; salvaging partials"
            )
            try:
                analysis = _salvage_analysis(text)
                partial = True
                ANALYSIS_TRUNCATIONS.labels(outcome="salvaged").inc()
            except (ValueError, ValidationError) as exc:
                ANALYSIS_TRUNCATIONS.labels(outcome="failed").inc()
                raise InvalidAnalysisResult(cause=exc) from exc
        return SecurityAnalysisResponse(
            filename=_display_filename(filename),
            partial=partial,
            **analysis.model_dump(),
        )

    async def _invoke(self, messages: list[Any]) -> Any:
        try:
            return await asyncio.wait_for(
                self._model.ainvoke(messages),
                timeout=self._settings.analysis_timeout_seconds,
            )
        except TimeoutError as exc:
            raise RequestTimedOut(cause=exc) from exc
        except OpenAIAPIError as exc:
            raise UpstreamFailed(cause=exc) from exc
