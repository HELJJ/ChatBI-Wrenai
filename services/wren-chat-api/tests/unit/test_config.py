from pathlib import Path

import pytest
from pydantic import ValidationError

from wren_chat_api.config import Settings


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "state_database_url": "postgresql://chat:secret@localhost:5432/wren_chat",
        "api_key": "private-api-key",
        "project_path": tmp_path,
        "model": "test-model",
    }
    values.update(overrides)
    return Settings(**values)


def test_required_settings_must_be_provided(monkeypatch) -> None:
    for name in (
        "WREN_CHAT_STATE_DATABASE_URL",
        "WREN_CHAT_API_KEY",
        "WREN_CHAT_PROJECT_PATH",
        "WREN_CHAT_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        Settings()


def test_settings_use_confirmed_defaults(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    assert settings.question_max_chars == 4_000
    assert settings.default_row_limit == 100
    assert settings.max_row_limit == 1_000
    assert settings.max_result_bytes == 1_048_576
    assert settings.max_tool_content_bytes == 65_536
    assert settings.max_sql_attempts == 3
    assert settings.graph_recursion_limit == 12
    assert settings.request_timeout_seconds == 120
    assert settings.lease_ttl_seconds == 30
    assert settings.lease_renew_seconds == 10
    assert settings.interruption_threshold_seconds == 150
    assert settings.recent_turns == 6
    assert settings.wren_workers == 16
    assert settings.wren_queue_capacity == 32
    assert settings.recovery_interval_seconds == 30


def test_environment_uses_state_database_name(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "WREN_CHAT_STATE_DATABASE_URL",
        "postgresql://chat:secret@localhost:5432/wren_chat",
    )
    monkeypatch.setenv("WREN_CHAT_API_KEY", "api-secret")
    monkeypatch.setenv("WREN_CHAT_PROJECT_PATH", str(tmp_path))
    monkeypatch.setenv("WREN_CHAT_MODEL", "test-model")

    settings = Settings()

    assert settings.state_database_url.get_secret_value().endswith("/wren_chat")


@pytest.mark.parametrize(
    "state_database_url",
    [
        "not-a-dsn",
        "mysql://chat:secret@localhost:3306/wren_chat",
        "postgresql://chat:secret@localhost:5432",
    ],
)
def test_state_database_requires_postgresql_database_name(
    tmp_path: Path, state_database_url: str
) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, state_database_url=state_database_url)


def test_sensitive_settings_are_not_exposed_in_repr(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    rendered = repr(settings)
    assert "private-api-key" not in rendered
    assert "chat:secret" not in rendered


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"default_row_limit": 101, "max_row_limit": 100},
            "default_row_limit",
        ),
        (
            {"max_result_bytes": 1_024, "max_tool_content_bytes": 2_048},
            "max_tool_content_bytes",
        ),
        (
            {"lease_ttl_seconds": 30, "lease_renew_seconds": 30},
            "lease_renew_seconds",
        ),
        (
            {
                "request_timeout_seconds": 120,
                "interruption_threshold_seconds": 120,
            },
            "interruption_threshold_seconds",
        ),
    ],
)
def test_related_limits_are_consistent(
    tmp_path: Path, overrides: dict, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(tmp_path, **overrides)
