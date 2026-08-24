"""LLM-backed risk self-check: match component+version pairs against their
vulnerability descriptions and report, per batch entry, whether any of them
hits."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from openai import APIError as OpenAIAPIError

from wren_chat_api.config import Settings
from wren_chat_api.contracts import (
    RiskSelfCheckErrorItem,
    RiskSelfCheckItem,
    RiskSelfCheckRequest,
    RiskSelfCheckResponse,
    RiskSelfCheckResultItem,
)
from wren_chat_api.errors import (
    InvalidRiskCheckResult,
    RequestTimedOut,
    UpstreamFailed,
)

logger = logging.getLogger(__name__)

# The model only answers with hit indices, never rewrites the descriptions:
# the server maps indices back to a 0/1 verdict, so a hallucinated match
# text can never leak into the response, and an out-of-range index is a
# hard validation failure instead of a silent wrong answer.
_SYSTEM_PROMPT = """\
你是一名漏洞情报分析专家。

用户会提供：待核查的组件名称（component）、该组件的版本（version），\
以及一组从 0 开始编号的漏洞描述。

任务：逐条判断每条漏洞描述是否影响该组件的该版本。判定规则：

1. 组件匹配：描述声称的受影响组件与输入组件是同一软件。注意命名别名、\
大小写与厂商前缀差异，例如 log4j 与 Apache Log4j2、struts2 与 \
Apache Struts 是同一组件；名称相似但不同的软件（如 log4j 与 logback）\
不是。
2. 版本匹配：描述中的受影响版本范围包含输入版本。范围表述可能是开闭\
区间、列表、"及之前/之后"、"所有版本"等；按语义判断，例如输入 2.3.31 \
落在 "2.3.5 - 2.3.31" 内。
3. 仅当组件与版本同时匹配才算命中。描述未注明受影响版本范围时，组件\
匹配即视为命中（安全自查宁可多报不可漏报）。

严格只输出一个 JSON 对象：不要使用 Markdown 代码围栏，\
不要输出任何解释性文字。JSON 结构如下：
{"matched_indices": [命中的描述编号，按升序]}

没有任何命中时输出 {"matched_indices": []}。"""


def _build_user_prompt(item: RiskSelfCheckItem) -> str:
    lines = [
        f"组件：{item.component}",
        f"版本：{item.version}",
        f"漏洞描述（共 {len(item.vulnerability_descriptions)} 条）：",
    ]
    lines.extend(
        f"[{index}] {description}"
        for index, description in enumerate(item.vulnerability_descriptions)
    )
    return "\n".join(lines)


def _parse_indices(text: str, count: int) -> set[int]:
    """Validate the model output and return the matched index set.

    Any deviation — missing/malformed JSON, non-integer entries, booleans,
    duplicates folded silently is fine but out-of-range indices are not —
    raises so the caller can fail the item closed.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object found in model response")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ValueError("model response is not valid JSON") from exc
    if not isinstance(data, dict) or "matched_indices" not in data:
        raise ValueError("model response lacks matched_indices")
    raw = data["matched_indices"]
    if not isinstance(raw, list):
        raise ValueError("matched_indices is not a list")
    indices: set[int] = set()
    for item in raw:
        # bool is an int subclass; a bare true/false must not pass as an index.
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError("matched_indices contains a non-integer entry")
        if not 0 <= item < count:
            raise ValueError(f"matched index {item} out of range for {count} items")
        indices.add(item)
    return indices


class RiskSelfCheckService:
    """Judge each batch entry's component+version against its descriptions."""

    def __init__(self, *, model: Any, settings: Settings) -> None:
        # The verdict is a tiny JSON array, so a small explicit ceiling is
        # ample; it also stops gateways from truncating at odd defaults.
        self._model = model.bind(max_tokens=settings.risk_selfcheck_max_tokens)
        self._settings = settings

    async def check(self, item: RiskSelfCheckItem) -> Literal[0, 1]:
        """Return the 0/1 verdict for one entry, or raise a typed error."""
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_build_user_prompt(item)),
        ]
        response = await self._invoke(messages)
        text = str(response.content)
        try:
            indices = _parse_indices(text, len(item.vulnerability_descriptions))
        except ValueError as exc:
            logger.warning(
                "risk self-check output failed validation (%s); failing closed",
                exc,
            )
            raise InvalidRiskCheckResult(cause=exc) from exc
        return 1 if indices else 0

    async def check_batch(self, request: RiskSelfCheckRequest) -> RiskSelfCheckResponse:
        """Judge every entry concurrently, tolerating per-entry failures.

        A failed entry comes back as an ``error`` item instead of being
        dropped or masked as matched=0; results keep request order.
        """
        semaphore = asyncio.Semaphore(self._settings.risk_selfcheck_concurrency)

        async def run(item: RiskSelfCheckItem) -> RiskSelfCheckResultItem | RiskSelfCheckErrorItem:
            async with semaphore:
                try:
                    matched = await self.check(item)
                except (
                    InvalidRiskCheckResult,
                    UpstreamFailed,
                    RequestTimedOut,
                ) as exc:
                    logger.warning(
                        "risk self-check item %s failed: %s",
                        item.id,
                        type(exc).__name__,
                    )
                    return RiskSelfCheckErrorItem(
                        id=item.id,
                        error=exc.public_message,
                    )
                return RiskSelfCheckResultItem(
                    id=item.id,
                    component=item.component,
                    matched=matched,
                )

        entries = await asyncio.gather(*(run(item) for item in request.items))
        return RiskSelfCheckResponse(data=list(entries))

    async def _invoke(self, messages: list[Any]) -> Any:
        try:
            return await asyncio.wait_for(
                self._model.ainvoke(messages),
                timeout=self._settings.risk_selfcheck_timeout_seconds,
            )
        except TimeoutError as exc:
            raise RequestTimedOut(cause=exc) from exc
        except OpenAIAPIError as exc:
            raise UpstreamFailed(cause=exc) from exc
