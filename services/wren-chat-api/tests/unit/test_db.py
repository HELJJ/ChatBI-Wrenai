"""Unit contracts for PostgreSQL pool construction."""

from pathlib import Path

import pytest
from psycopg.rows import dict_row

from wren_chat_api.audit import AuditRepository
from wren_chat_api.config import Settings
from wren_chat_api.db import (
    apply_migrations,
    create_app_pool,
    create_checkpoint_pool,
    default_migrations_dir,
)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        state_database_url=(
            "postgresql://chat:test%40password@127.0.0.1:5432/wren_chat"
        ),
        api_key="private-api-key",
        project_path=tmp_path,
        model="test-model",
    )


def test_app_pool_uses_transactional_dict_connections(tmp_path: Path) -> None:
    pool = create_app_pool(make_settings(tmp_path))

    assert pool.name == "wren-chat-app"
    assert pool.kwargs == {"row_factory": dict_row}
    assert pool.closed


def test_checkpoint_pool_has_isolated_checkpointer_semantics(
    tmp_path: Path,
) -> None:
    pool = create_checkpoint_pool(make_settings(tmp_path))

    assert pool.name == "wren-chat-checkpoint"
    # Tuple rows (no dict_row): the checkpointer unpacks rows positionally.
    assert pool.kwargs == {
        "autocommit": True,
        "prepare_threshold": 0,
    }
    assert pool.closed


def test_default_migrations_directory_contains_initial_schema() -> None:
    migration = default_migrations_dir() / "0001_chat_audit.sql"

    assert migration.is_file()


async def test_migrations_require_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Migration directory"):
        await apply_migrations(None, tmp_path / "missing")


async def test_migrations_require_at_least_one_sql_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="No SQL migrations"):
        await apply_migrations(None, tmp_path)


@pytest.mark.parametrize("maximum", [0, 4])
def test_audit_repository_enforces_hard_attempt_limit(maximum: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 3"):
        AuditRepository(None, max_sql_attempts=maximum)
