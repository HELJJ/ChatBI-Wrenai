"""Unit contracts for bounded result normalization and tool content."""

from decimal import Decimal

import pyarrow as pa
import pytest

from wren_chat_api.errors import ResultTooLarge
from wren_chat_api.results import (
    build_attempt_result,
    build_tool_content,
    normalize_json,
    redact_secrets,
)


def test_n_plus_one_row_is_removed_and_marks_truncated() -> None:
    table = pa.table({"id": [1, 2, 3]})
    result = build_attempt_result(table, row_limit=2, max_bytes=10_000)

    assert result.returned_row_count == 2
    assert result.result_truncated is True
    assert result.result["rows"] == [{"id": 1}, {"id": 2}]


def test_decimal_is_string_and_nonfinite_float_is_null() -> None:
    normalized = normalize_json(
        {"amount": Decimal("1.20"), "ratio": float("nan")}
    )

    assert normalized == {"amount": "1.20", "ratio": None}


def test_oversized_single_row_raises_result_too_large() -> None:
    table = pa.table({"payload": ["x" * 2_000]})
    with pytest.raises(ResultTooLarge):
        build_attempt_result(table, row_limit=100, max_bytes=1_024)


def test_tool_content_cap_does_not_change_audit_truncation() -> None:
    table = pa.table({"payload": ["x" * 1_000, "y" * 1_000]})
    result = build_attempt_result(table, row_limit=100, max_bytes=10_000)
    tool_content = build_tool_content(result, max_bytes=512)

    assert result.returned_row_count == 2
    assert result.result_truncated is False
    assert tool_content.content_truncated is True


def test_tool_content_fits_within_max_bytes() -> None:
    table = pa.table({"payload": ["x" * 1_000, "y" * 1_000]})
    result = build_attempt_result(table, row_limit=100, max_bytes=10_000)
    tool_content = build_tool_content(result, max_bytes=512)

    assert len(tool_content.content.encode()) <= 512
    assert "aggregate" in tool_content.content or "filter" in tool_content.content


def test_datetime_and_uuid_are_serializable_strings() -> None:
    from datetime import datetime, timezone
    from uuid import uuid4

    stamp = datetime(2026, 8, 14, 8, 30, tzinfo=timezone.utc)
    identifier = uuid4()
    normalized = normalize_json({"stamp": stamp, "id": identifier})

    assert normalized == {"stamp": stamp.isoformat(), "id": str(identifier)}


def test_redact_secrets_masks_sensitive_keys() -> None:
    value = {
        "user": "analyst",
        "api_key": "sk-123",
        "nested": {"password": "hunter2", "count": 2},
    }

    redacted = redact_secrets(value)

    assert redacted == {
        "user": "analyst",
        "api_key": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "count": 2},
    }
