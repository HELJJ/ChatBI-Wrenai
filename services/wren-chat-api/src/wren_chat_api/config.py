"""Validated runtime configuration for the Wren chat API."""

from __future__ import annotations

from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed service settings with bounded operational defaults."""

    model_config = SettingsConfigDict(
        env_prefix="WREN_CHAT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    state_database_url: SecretStr
    api_key: SecretStr
    project_path: Path
    model: str = Field(min_length=1)
    model_api_key: SecretStr | None = None
    model_base_url: str | None = None
    # sqlglot dialect used by the read-only SQL gate. Must match the project's
    # data source (e.g. "oracle" for DM8, "postgres" for PostgreSQL).
    sql_dialect: str = Field(default="postgres", min_length=1)
    # Vendor extension (DashScope qwen3): disable reasoning in agent loops by
    # default; thinking slows tool-calling and breaks non-streaming responses.
    model_enable_thinking: bool = False

    question_max_chars: int = Field(default=4_000, ge=1, le=4_000)
    default_row_limit: int = Field(default=100, ge=1, le=1_000)
    max_row_limit: int = Field(default=1_000, ge=1, le=1_000)
    max_result_bytes: int = Field(default=1_048_576, ge=1_024)
    max_tool_content_bytes: int = Field(default=65_536, ge=1_024)
    # SQL budget sized for: 1 schema probe + several fix-and-retry rounds +
    # the real analytical queries. Observed field failures (kylin-006/007)
    # burned 3 attempts probing an empty table / hallucinated columns before
    # the correct query ever ran.
    max_sql_attempts: int = Field(default=50, ge=1, le=100)
    # Recursion must cover ~2 steps per agent round (model + tools) plus the
    # final answer turn; with max_sql_attempts=50 and 2-3 context tools the
    # graph can legitimately need 60+ steps.
    graph_recursion_limit: int = Field(default=300, ge=2, le=600)
    # Must exceed the time 50 SQL attempts + ~150 model calls can take; the
    # old 120s cap truncated runs before the raised budgets above applied.
    request_timeout_seconds: int = Field(default=600, ge=1, le=1_800)
    lease_ttl_seconds: int = Field(default=30, ge=2)
    lease_renew_seconds: int = Field(default=10, ge=1)
    interruption_threshold_seconds: int = Field(default=750, ge=1)
    recent_turns: int = Field(default=6, ge=1, le=6)
    wren_workers: int = Field(default=16, ge=1)
    wren_queue_capacity: int = Field(default=32, ge=0)
    recovery_interval_seconds: int = Field(default=30, ge=1)
    # Upload gate for the security-report analysis endpoint: markdown check
    # reports are small, so 1 MiB is generous while capping abuse.
    max_report_bytes: int = Field(default=1_048_576, ge=1_024, le=10_485_760)
    # One LLM pass over a single report; far below the multi-round chat budget.
    analysis_timeout_seconds: int = Field(default=120, ge=1, le=600)
    # Output ceiling for one analysis pass. The observed MaaS gateway silently
    # truncated generation at 5120 tokens (finish_reason=length) mid-JSON
    # because no explicit max_tokens was sent, so set one explicitly.
    analysis_max_tokens: int = Field(default=8_192, ge=1_024, le=32_768)
    # Continuation rounds appended after a length-truncated response before
    # falling back to salvaging the partial output.
    analysis_max_continuations: int = Field(default=3, ge=0, le=5)
    # --- Risk self-check (component+version vs vulnerability descriptions) ---
    # One LLM pass per batch entry; the verdict JSON is tiny.
    risk_selfcheck_timeout_seconds: int = Field(default=120, ge=1, le=600)
    risk_selfcheck_max_tokens: int = Field(default=1_024, ge=64, le=8_192)
    # Concurrent per-entry model calls; kept low to stay under MaaS rate
    # limits while still finishing a 50-entry batch in reasonable time.
    risk_selfcheck_concurrency: int = Field(default=4, ge=1, le=32)

    # --- Pentest-record extraction (PDF risk-item pipeline) ---
    # All four model knobs fall back to the chat model settings, so a
    # deployment whose chat model is already multimodal (e.g. qwen3.7-plus
    # via DashScope) needs no extra configuration. Point them at an intranet
    # OpenAI-compatible endpoint (e.g. vLLM serving GLM-4.6V) to switch.
    pentest_vlm_model: str | None = None
    pentest_vlm_base_url: str | None = None
    pentest_vlm_api_key: SecretStr | None = None
    # OCR channel for scanned pages (text fallback only, never tables);
    # defaults to the extraction VLM itself, which can transcribe pages.
    pentest_ocr_model: str | None = None
    # Vendor switch (DashScope qwen3): non-streaming calls must explicitly
    # disable thinking mode; same reasoning as model_enable_thinking.
    pentest_vlm_enable_thinking: bool = False
    pentest_render_dpi: int = Field(default=150, ge=72, le=400)
    pentest_page_concurrency: int = Field(default=4, ge=1, le=16)
    pentest_doc_concurrency: int = Field(default=2, ge=1, le=8)

    # --- Risk-assessment report extraction (风险等级统计 pipeline) ---
    # One LLM pass over the serialized section; the output is eight tiny
    # fields, so the ceiling only needs headroom over that.
    risk_assessment_timeout_seconds: int = Field(default=120, ge=1, le=600)
    risk_assessment_max_tokens: int = Field(default=512, ge=64, le=4_096)
    # Legacy .doc front-end: headless LibreOffice, one conversion at a time.
    risk_assessment_soffice_bin: str = Field(default="soffice", min_length=1)
    risk_assessment_convert_timeout_seconds: int = Field(
        default=60, ge=1, le=300
    )

    @field_validator("state_database_url")
    @classmethod
    def validate_state_database_url(cls, value: SecretStr) -> SecretStr:
        """Require a psycopg-compatible PostgreSQL DSN with a database name."""
        raw_value = value.get_secret_value()
        if "://" in raw_value:
            parsed_url = urlsplit(raw_value)
            if parsed_url.scheme not in {"postgres", "postgresql"}:
                raise ValueError("state_database_url must use PostgreSQL")

        try:
            conninfo = conninfo_to_dict(raw_value)
        except Exception as exc:
            raise ValueError(
                "state_database_url must be a valid PostgreSQL DSN"
            ) from exc

        if not conninfo.get("dbname"):
            raise ValueError("state_database_url must include a database name")
        return value

    @model_validator(mode="after")
    def validate_related_limits(self) -> Self:
        if self.default_row_limit > self.max_row_limit:
            raise ValueError("default_row_limit must not exceed max_row_limit")
        if self.max_tool_content_bytes > self.max_result_bytes:
            raise ValueError("max_tool_content_bytes must not exceed max_result_bytes")
        if self.lease_renew_seconds >= self.lease_ttl_seconds:
            raise ValueError("lease_renew_seconds must be less than lease_ttl_seconds")
        if self.interruption_threshold_seconds <= self.request_timeout_seconds:
            raise ValueError(
                "interruption_threshold_seconds must exceed request_timeout_seconds"
            )
        return self
