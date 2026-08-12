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
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    state_database_url: SecretStr
    api_key: SecretStr
    project_path: Path
    model: str = Field(min_length=1)

    question_max_chars: int = Field(default=4_000, ge=1, le=4_000)
    default_row_limit: int = Field(default=100, ge=1, le=1_000)
    max_row_limit: int = Field(default=1_000, ge=1, le=1_000)
    max_result_bytes: int = Field(default=1_048_576, ge=1_024)
    max_tool_content_bytes: int = Field(default=65_536, ge=1_024)
    max_sql_attempts: int = Field(default=3, ge=1, le=3)
    graph_recursion_limit: int = Field(default=12, ge=2, le=12)
    request_timeout_seconds: int = Field(default=120, ge=1, le=120)
    lease_ttl_seconds: int = Field(default=30, ge=2)
    lease_renew_seconds: int = Field(default=10, ge=1)
    interruption_threshold_seconds: int = Field(default=150, ge=1)
    recent_turns: int = Field(default=6, ge=1, le=6)
    wren_workers: int = Field(default=16, ge=1)
    wren_queue_capacity: int = Field(default=32, ge=0)
    recovery_interval_seconds: int = Field(default=30, ge=1)

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
