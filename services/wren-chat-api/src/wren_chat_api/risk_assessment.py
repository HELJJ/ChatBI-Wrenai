"""Risk-level statistics extraction from uploaded risk-assessment reports.

Pipeline: the upload is normalized to DOCX first (legacy OLE2 ``.doc`` files
go through one headless LibreOffice conversion), then stdlib-only parsing
(``zipfile`` + ``ElementTree``, no new dependencies) turns the document into
an ordered block stream of paragraphs and tables. A structural gate delimits
the 风险等级统计 section — anchored on its heading whatever the section
number, ended at the first same-or-higher-level heading — so the earlier,
structurally identical 风险等级划分表 (five levels, no count columns) never
reaches the model. One shared-model LLM call over the serialized section
(paragraphs verbatim, tables as markdown) produces the eight contract
fields, and a deterministic validation layer enforces containment against
the text channel: every count, every rate's percentage form, and the final
evaluation name must literally occur in the section, and the summary
sentence ("共发现X个高风险，Y个中风险，Z个低风险") must agree with the
extracted counts when it is parseable at all.

Two calibers are deliberate contract decisions, not accidents: the
five-level statistics table is collapsed to three buckets (很高/很低 rows
are ignored, rates come from the table's own percentage column), and any
validation failure fails closed — a number that cannot be traced back to
the document is an error, never a fabricated zero.

Heading numbers in this report family are Word auto-numbering (``numPr``):
the rendered "3.4" never occurs in the text layer, so heading detection
keys on paragraph shape (style outline level / numbering properties /
numbered short line), not on digits in the text.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from openai import APIError as OpenAIAPIError

from wren_chat_api.config import Settings
from wren_chat_api.contracts import RiskAssessmentExtractResponse
from wren_chat_api.errors import (
    InvalidRiskAssessmentResult,
    InvalidRiskFile,
    RequestTimedOut,
    RiskDocConversionFailed,
    UpstreamFailed,
)
from wren_chat_api.pentest_extract import display_filename

logger = logging.getLogger(__name__)

_ALLOWED_SUFFIXES = frozenset({".doc", ".docx"})
_ZIP_MAGIC = b"PK"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

SECTION_TITLE = "风险等级统计"
# A decompressed-member cap: unlike the PDF pipeline (cost scales with page
# rendering), a zip bomb here feeds inflated XML straight into the parser.
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
# Whole-document fallback contexts are small in this report family; the cap
# only guards a pathological upload from blowing the model context.
_MAX_PROMPT_CHARS = 200_000

# TOC lines (dot leaders + page number) name sections without being them.
TOC_LINE_RE = re.compile(r"[.·…]{2,}\s*\d+\s*$")
# Manually numbered heading line: "3.4 风险等级统计" (separator required so
# prose starting with a bare number is less likely to match).
NUMBERED_LINE_RE = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2}){0,3}[\s\u3000]+\S.{0,38}$")
_LEADING_NUMBER_RE = re.compile(r"^\d{1,2}(?:\.\d{1,2}){0,3}")
_SENTENCE_END_RE = re.compile(r"[。！？；;]")
# Summary-sentence cross-check, matched on space-stripped text: the prose
# restates the three bucket counts and must agree with the extracted ones.
_PROSE_COUNTS_RE = re.compile(
    r"(\d+)个高风险[，,](\d+)个中(?:等)?风险[，,](\d+)个低风险"
)
_CODE_TO_NAME = {"H": "高风险", "M": "中风险", "L": "低风险"}
_COUNT_FIELDS = ("riskHigh", "riskMedium", "riskLow")
_RATE_FIELDS = ("riskHighRate", "riskMediumRate", "riskLowRate")

_SYSTEM_PROMPT = """\
你是一名风险评估报告解析专家。

用户会提供一份中文风险评估报告中「风险等级统计」章节的文本（若章节\
定位失败则为整篇文档）。表格以 Markdown 表格呈现。从中提取以下字段：

1. riskHigh / riskMedium / riskLow：统计表中风险标识为 高（或高风险）、\
中等（或中、中风险）、低（或低风险）的行的「个数」列数值。\
很高/极高 与 很低/极低 等其他档位的行一律忽略，不并入任何一档。
2. riskHighRate / riskMediumRate / riskLowRate：对应行的百分比数值除以\
100（例如 2% → 0.02）。
3. finalEvaluationName：文中最终评价结论（如"最终判定/评价/结论为\
X风险"）中的 X风险，取值为 高风险/中风险/低风险 之一；\
finalEvaluationCode 按映射输出：高→H、中→M、低→L。

判定规则：
- 数值必须来自原文，禁止推算、合计或编造；若某个字段在原文中不存在\
或无法确定，输出 null，不要猜测。
- 章节定位失败时只在全文中属于「风险等级统计」的统计表与结论句中\
取数；其他章节（如风险等级划分表，其只有风险值区间与描述、没有\
个数/百分比列）不是数据来源。

严格只输出一个 JSON 对象：不要使用 Markdown 代码围栏，不要输出任何\
解释性文字。JSON 结构如下：
{"riskHigh": null, "riskHighRate": null, "riskMedium": null, \
"riskMediumRate": null, "riskLow": null, "riskLowRate": null, \
"finalEvaluationCode": null, "finalEvaluationName": null}"""


def validate_risk_upload(filename: str | None, raw: bytes) -> None:
    """Accept only uploadable risk-assessment documents (.doc/.docx)."""
    if filename is None or Path(filename).suffix.lower() not in _ALLOWED_SUFFIXES:
        raise InvalidRiskFile("filename")
    head = raw.lstrip()[:8]
    if not (head.startswith(_ZIP_MAGIC) or head.startswith(_OLE_MAGIC)):
        raise InvalidRiskFile("missing zip/OLE2 header")


# --------------------------------------------------------------- parsing
@dataclass(frozen=True)
class Block:
    """One body-level element: a paragraph or a table in document order."""

    kind: str  # "p" | "tbl"
    text: str = ""
    rows: tuple[tuple[str, ...], ...] = ()
    # Heading rank (smaller = higher) when the paragraph is a heading
    # candidate, else None. Style outline levels (0-8) and numbering-family
    # ranks (100+ilvl) are comparable within a family; documents use one
    # family consistently.
    heading_rank: int | None = field(default=None)


def _para_text(p: ET.Element) -> str:
    parts: list[str] = []
    for node in p.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
    return "".join(parts)


def _table_rows(tbl: ET.Element) -> tuple[tuple[str, ...], ...]:
    rows = []
    for tr in tbl.findall(f"{_W}tr"):
        cells = []
        for tc in tr.findall(f"{_W}tc"):
            cells.append(
                " ".join(_para_text(p).strip() for p in tc.findall(f"{_W}p")).strip()
            )
        rows.append(tuple(cells))
    return tuple(rows)


def _style_outline_levels(styles_xml: bytes) -> tuple[dict[str, int], dict[str, str]]:
    """Map style id -> outline level and style id -> based-on parent id."""
    levels: dict[str, int] = {}
    parents: dict[str, str] = {}
    name_level: dict[str, int] = {}
    root = ET.fromstring(styles_xml)
    for style in root.findall(f"{_W}style"):
        style_id = style.get(f"{_W}styleId")
        if not style_id:
            continue
        based = style.find(f"{_W}basedOn")
        if based is not None and based.get(f"{_W}val"):
            parents[style_id] = based.get(f"{_W}val")
        outline = style.find(f"{_W}pPr/{_W}outlineLvl")
        if outline is not None:
            try:
                levels[style_id] = int(outline.get(f"{_W}val"))
                continue
            except (TypeError, ValueError):
                pass
        name = style.find(f"{_W}name")
        if name is not None:
            match = re.match(
                r"^(?:heading|标题)\s*(\d)$", (name.get(f"{_W}val") or "").strip()
            )
            if match:
                name_level[style_id] = int(match.group(1)) - 1
    levels.update(name_level)
    return levels, parents


def _resolve_outline_level(
    style_id: str | None, styles: dict[str, int], parents: dict[str, str]
) -> int | None:
    seen: set[str] = set()
    current = style_id
    for _ in range(6):  # basedOn chains are shallow in practice
        if not current or current in seen:
            return None
        seen.add(current)
        if current in styles:
            return styles[current]
        current = parents.get(current)
    return None


def _looks_like_heading_text(text: str) -> bool:
    stripped = text.strip()
    return (
        bool(stripped) and len(stripped) <= 40 and not _SENTENCE_END_RE.search(stripped)
    )


def _heading_rank(
    p: ET.Element, text: str, styles: dict[str, int], parents: dict[str, str]
) -> int | None:
    """Heading rank of a paragraph, or None when it is body text.

    Style outline level wins; Word auto-numbered short paragraphs (numPr)
    and manually numbered short lines form a second, lower family.
    """
    pPr = p.find(f"{_W}pPr")
    if TOC_LINE_RE.search(text):
        return None
    if not _looks_like_heading_text(text):
        return None
    if pPr is not None:
        pStyle = pPr.find(f"{_W}pStyle")
        style_id = pStyle.get(f"{_W}val") if pStyle is not None else None
        outline = _resolve_outline_level(style_id, styles, parents)
        if outline is not None:
            return outline
        numPr = pPr.find(f"{_W}numPr")
        if numPr is not None and text.strip():
            ilvl = numPr.find(f"{_W}ilvl")
            try:
                level = int(ilvl.get(f"{_W}val")) if ilvl is not None else 0
            except (TypeError, ValueError):
                level = 0
            return 100 + level
    if NUMBERED_LINE_RE.match(text):
        number = _LEADING_NUMBER_RE.match(text.strip())
        depth = number.group(0).count(".") if number else 0
        return 100 + depth
    return None


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    with zf.open(name) as member:
        data = member.read(_MAX_MEMBER_BYTES + 1)
    if len(data) > _MAX_MEMBER_BYTES:
        raise InvalidRiskFile(f"{name} decompresses beyond cap")
    return data


def parse_docx_blocks(raw: bytes) -> list[Block]:
    """Ordered paragraph/table blocks of one DOCX payload."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise InvalidRiskFile("unreadable docx zip", cause=exc) from exc
    with zf:
        try:
            document = _read_member(zf, "word/document.xml")
        except KeyError as exc:
            raise InvalidRiskFile("missing word/document.xml") from exc
        try:
            styles_xml = _read_member(zf, "word/styles.xml")
        except KeyError:
            styles_xml = b""
    try:
        levels, parents = _style_outline_levels(styles_xml) if styles_xml else ({}, {})
        root = ET.fromstring(document)
    except ET.ParseError as exc:
        raise InvalidRiskFile("malformed document xml", cause=exc) from exc

    body = root.find(f"{_W}body")
    if body is None:
        raise InvalidRiskFile("missing body")
    blocks: list[Block] = []
    for child in body:
        if child.tag == f"{_W}p":
            text = _para_text(child)
            blocks.append(
                Block(
                    kind="p",
                    text=text,
                    heading_rank=_heading_rank(child, text, levels, parents),
                )
            )
        elif child.tag == f"{_W}tbl":
            blocks.append(Block(kind="tbl", rows=_table_rows(child)))
    return blocks


# ----------------------------------------------------------------- gating
def _title_matches(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    compact = _LEADING_NUMBER_RE.sub("", compact, count=1)
    return compact == SECTION_TITLE


def gate_section(blocks: list[Block]) -> tuple[list[Block], str]:
    """Delimit the 风险等级统计 section, or fall back to the whole document.

    The start is the section heading (any numbering, rendered or manual);
    the section ends right before the first same-or-higher-ranked heading.
    """
    start = None
    start_rank = None
    for index, block in enumerate(blocks):
        if block.kind == "p" and block.heading_rank is not None:
            if _title_matches(block.text):
                start = index
                start_rank = block.heading_rank
                break
    if start is None:
        return blocks, "全文档"
    end = len(blocks)
    for index in range(start + 1, len(blocks)):
        block = blocks[index]
        if block.kind == "p" and block.heading_rank is not None:
            if block.heading_rank <= start_rank:
                end = index
                break
    return blocks[start:end], "章节定位"


def serialize_blocks(blocks: list[Block]) -> str:
    """Render blocks for the model: paragraphs verbatim, tables as markdown."""
    lines: list[str] = []
    for block in blocks:
        if block.kind == "p":
            if block.text.strip():
                lines.append(block.text.strip())
        else:
            for index, row in enumerate(block.rows):
                cells = [cell or " " for cell in row]
                lines.append("| " + " | ".join(cells) + " |")
                if index == 0:
                    lines.append("|" + " --- |" * len(cells))
            lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------- validation
def parse_extraction(raw: str) -> dict[str, Any]:
    """Defensively parse the model output; raises ValueError when malformed."""
    text = (raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object found in model response")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ValueError("model response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("model response is not a JSON object")
    return data


_COUNT_RATE_PAIRS = (
    ("riskHigh", "riskHighRate"),
    ("riskMedium", "riskMediumRate"),
    ("riskLow", "riskLowRate"),
)


def validate_extraction(payload: dict[str, Any], section_text: str) -> dict[str, Any]:
    """Validate the eight fields against the section text channel.

    Returns the validated mapping on success; raises InvalidRiskAssessmentResult
    with every problem listed. Semantic pointing (which table row is 高) stays
    with the model; this layer only enforces existence, domains, and
    cross-source agreement, so template drift never turns rules into a new
    failure mode.

    Count containment alone is too weak: single-digit counts collide with
    the table's 风险等级 column (5/4/3/2/1). When the section has any table,
    each (count, percent) pair must co-occur in one table row instead.
    """
    errors: list[str] = []
    compact = re.sub(r"\s+", "", section_text).replace("％", "%")
    stats: dict[str, Any] = {}

    for key in (
        *_COUNT_FIELDS,
        *_RATE_FIELDS,
        "finalEvaluationCode",
        "finalEvaluationName",
    ):
        if payload.get(key) is None:
            errors.append(f"缺少字段 {key}（模型未在原文中找到）")

    table_rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section_text.splitlines()
        if line.strip().startswith("|")
    ]

    if not errors:
        for count_key, rate_key in _COUNT_RATE_PAIRS:
            count, rate = payload[count_key], payload[rate_key]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                errors.append(f"{count_key} 不是非负整数")
                continue
            if (
                not isinstance(rate, (int, float))
                or isinstance(rate, bool)
                or not 0 <= rate <= 1
            ):
                errors.append(f"{rate_key} 不在 [0,1] 内")
                continue
            percent = f"{round(rate * 100):g}"
            if table_rows:
                if not any(
                    str(count) in row
                    and any(cell in (f"{percent}%", f"{percent}.0%") for cell in row)
                    for row in table_rows
                ):
                    errors.append(
                        f"{count_key}={count} 与 {rate_key}={rate} "
                        f"未同现于统计表同一行(疑似幻觉)"
                    )
                    continue
            elif str(count) not in compact or f"{percent}%" not in compact:
                errors.append(
                    f"{count_key}={count} 或其百分比形式 {percent}% "
                    f"未出现在文本通道(疑似幻觉)"
                )
                continue
            stats[count_key] = count
            stats[rate_key] = float(rate)

    if not errors:
        code = payload["finalEvaluationCode"]
        name = payload["finalEvaluationName"]
        if code not in _CODE_TO_NAME:
            errors.append(f"finalEvaluationCode 非枚举值: {code!r}")
        elif name != _CODE_TO_NAME[code]:
            errors.append(f"finalEvaluationCode({code}) 与 name({name}) 不对应")
        elif name not in compact:
            errors.append(f"finalEvaluationName={name} 未出现在文本通道(疑似幻觉)")
        else:
            stats["finalEvaluationCode"] = code
            stats["finalEvaluationName"] = name

    if not errors:
        prose = _PROSE_COUNTS_RE.search(compact)
        if prose:
            prose_counts = tuple(int(g) for g in prose.groups())
            model_counts = tuple(stats[key] for key in _COUNT_FIELDS)
            if prose_counts != model_counts:
                errors.append(
                    f"与总述句不一致: 原文 {prose_counts} vs 提取 {model_counts}"
                )

    if errors:
        raise InvalidRiskAssessmentResult("; ".join(errors))
    return stats


# ------------------------------------------------------- doc conversion
def run_soffice(raw: bytes, *, soffice_bin: str, timeout_seconds: int) -> bytes:
    """Convert one legacy .doc payload to DOCX via headless LibreOffice."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        source = tmp_dir / f"{uuid.uuid4().hex}.doc"
        source.write_bytes(raw)
        profile = tmp_dir / "profile"
        profile.mkdir()
        try:
            completed = subprocess.run(
                [
                    soffice_bin,
                    "--headless",
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    str(tmp_dir),
                    str(source),
                ],
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RiskDocConversionFailed("soffice binary not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise RiskDocConversionFailed("soffice conversion timed out") from exc
        converted = source.with_suffix(".docx")
        if completed.returncode != 0 or not converted.exists():
            raise RiskDocConversionFailed(
                f"soffice rc={completed.returncode}: "
                f"{(completed.stderr or b'').decode(errors='replace')[:200]}"
            )
        return converted.read_bytes()


# ---------------------------------------------------------------- service
class RiskAssessmentService:
    """Extract the 风险等级统计 statistics from one uploaded report."""

    def __init__(self, *, model: Any, settings: Settings) -> None:
        self._model = model.bind(
            temperature=0, max_tokens=settings.risk_assessment_max_tokens
        )
        self._settings = settings
        # soffice refuses concurrent runs sharing a profile; even with
        # per-run profiles, serializing keeps CPU spikes bounded.
        self._convert_lock = asyncio.Lock()

    async def extract(self, filename: str, raw: bytes) -> RiskAssessmentExtractResponse:
        started = time.monotonic()
        if raw.lstrip()[:8].startswith(_OLE_MAGIC):
            async with self._convert_lock:
                raw = await asyncio.to_thread(
                    run_soffice,
                    raw,
                    soffice_bin=self._settings.risk_assessment_soffice_bin,
                    timeout_seconds=(
                        self._settings.risk_assessment_convert_timeout_seconds
                    ),
                )
        blocks = await asyncio.to_thread(parse_docx_blocks, raw)
        section, gate_mode = gate_section(blocks)
        prompt_text = serialize_blocks(section)
        if len(prompt_text) > _MAX_PROMPT_CHARS:
            prompt_text = prompt_text[:_MAX_PROMPT_CHARS]
            logger.warning(
                "risk assessment prompt truncated to %d chars for %s",
                _MAX_PROMPT_CHARS,
                display_filename(filename),
            )

        try:
            payload = await self._invoke(prompt_text)
            stats = validate_extraction(payload, prompt_text)
        except InvalidRiskAssessmentResult as exc:
            logger.warning(
                "risk assessment validation failed for %s (%s mode): %s",
                display_filename(filename),
                gate_mode,
                exc.internal_message,
            )
            raise
        except (RequestTimedOut, UpstreamFailed) as exc:
            logger.warning(
                "risk assessment model call failed for %s: %s",
                display_filename(filename),
                type(exc).__name__,
            )
            raise

        logger.info(
            "risk assessment %s [%s]: %s in %.1fs",
            display_filename(filename),
            gate_mode,
            stats,
            time.monotonic() - started,
        )
        return RiskAssessmentExtractResponse(
            filename=display_filename(filename),
            **stats,
        )

    async def _invoke(self, prompt_text: str) -> dict[str, Any]:
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=prompt_text),
        ]
        try:
            response = await asyncio.wait_for(
                self._model.ainvoke(messages),
                timeout=self._settings.risk_assessment_timeout_seconds,
            )
        except TimeoutError as exc:
            raise RequestTimedOut(cause=exc) from exc
        except OpenAIAPIError as exc:
            raise UpstreamFailed(cause=exc) from exc
        try:
            return parse_extraction(str(response.content))
        except ValueError as exc:
            raise InvalidRiskAssessmentResult(cause=exc) from exc
