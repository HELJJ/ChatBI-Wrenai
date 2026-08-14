"""PyArrow result probing, truncation, JSON normalization, and redaction."""

from __future__ import annotations

import base64
import json
import math
import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

import pyarrow as pa
from pydantic import Field

from wren_chat_api.contracts import StrictModel
from wren_chat_api.errors import ResultTooLarge

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PATTERN = re.compile(
    r"password|passwd|secret|token|api[-_]?key|authorization|credential",
    re.IGNORECASE,
)

_TRUNCATION_INSTRUCTION = (
    " Tool content was truncated: aggregate, filter, or select fewer "
    "columns and query again."
)


class NormalizedResult(StrictModel):
    """Row-bounded and byte-bounded result persisted for one SQL attempt."""

    result: dict[str, Any]
    returned_row_count: int = Field(ge=0)
    result_truncated: bool


class ToolContent(StrictModel):
    """Byte-bounded content for the LLM, independent of the audit result."""

    content: str
    content_truncated: bool


def build_attempt_result(
    table: pa.Table,
    row_limit: int,
    max_bytes: int,
) -> NormalizedResult:
    """Normalize the first ``row_limit`` rows and enforce the byte ceiling.

    Truncation is derived from ``table.num_rows > row_limit`` and never from
    sampling. A payload that cannot fit within ``max_bytes`` raises
    ``ResultTooLarge`` so the attempt fails structurally and the model can
    narrow the query and retry.
    """
    if row_limit < 1:
        raise ValueError("row_limit must be positive")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")

    returned_rows = min(table.num_rows, row_limit)
    bounded = table.slice(0, row_limit)
    rows = [normalize_json(record) for record in bounded.to_pylist()]
    payload = {"columns": list(table.column_names), "rows": rows}

    serialized = _compact_json(payload)
    if len(serialized.encode()) > max_bytes:
        raise ResultTooLarge(
            f"Serialized result is {len(serialized.encode())} bytes, "
            f"above the {max_bytes} byte limit"
        )

    return NormalizedResult(
        result=payload,
        returned_row_count=returned_rows,
        result_truncated=table.num_rows > row_limit,
    )


def build_tool_content(result: NormalizedResult, max_bytes: int) -> ToolContent:
    """Fit column names, truncation flags, and leading rows into ``max_bytes``.

    The audit ``result_truncated`` flag is reported but never drives the
    content decision: the content budget is independent, and the full audit
    ``result`` is never serialized a second time here.
    """
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")

    header = _compact_json(
        {
            "columns": result.result["columns"],
            "result_truncated": result.result_truncated,
            "returned_row_count": result.returned_row_count,
        }
    )
    reserve = len(_TRUNCATION_INSTRUCTION.encode())

    content_parts: list[str] = [header]
    used = len(header.encode())
    content_truncated = False
    for row in result.result["rows"]:
        line = "\n" + _compact_json(row)
        line_size = len(line.encode())
        if used + line_size + reserve > max_bytes:
            content_truncated = True
            break
        content_parts.append(line)
        used += line_size

    content = "".join(content_parts)
    if content_truncated:
        content += _TRUNCATION_INSTRUCTION

    if len(content.encode()) > max_bytes:
        overflow_marker = "...[truncated]"
        keep = max_bytes - len(overflow_marker.encode())
        content = content.encode()[: max(keep, 0)].decode(errors="ignore")
        content += overflow_marker
        content_truncated = True

    return ToolContent(content=content, content_truncated=content_truncated)


def normalize_json(value: Any) -> Any:
    """Recursively convert a value into strict-JSON-compatible types."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode()
    if isinstance(value, dict):
        return {str(key): normalize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_json(item) for item in value]
    return str(value)


def redact_secrets(value: Any) -> Any:
    """Recursively mask values under sensitive-looking keys."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_KEY_PATTERN.search(str(key)):
                redacted[str(key)] = REDACTED
            else:
                redacted[str(key)] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
