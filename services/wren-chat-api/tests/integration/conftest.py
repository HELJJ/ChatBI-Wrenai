"""PostgreSQL fixtures for Wren chat API integration tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from importlib import import_module

import pytest
import pytest_asyncio

from wren_chat_api.audit import AuditRepository
from wren_chat_api.config import Settings
from wren_chat_api.db import apply_migrations, create_app_pool, default_migrations_dir


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Use a disposable test DSN or container; never point this at production."""
    configured_url = os.getenv("WREN_CHAT_TEST_DATABASE_URL")
    if configured_url:
        yield configured_url
        return

    try:
        postgres_module = import_module("testcontainers.postgres")
    except ImportError:
        pytest.skip(
            "PostgreSQL integration tests require WREN_CHAT_TEST_DATABASE_URL "
            "or testcontainers"
        )

    with postgres_module.PostgresContainer("postgres:15-alpine") as postgres:
        yield postgres.get_connection_url().replace("+psycopg2", "")


@pytest.fixture(scope="session")
def settings(postgres_url: str, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        state_database_url=postgres_url,
        api_key="integration-test-key",
        project_path=tmp_path_factory.mktemp("wren-project"),
        model="integration-test-model",
    )


@pytest_asyncio.fixture(scope="session")
async def app_pool(settings: Settings) -> AsyncIterator:
    pool = create_app_pool(settings)
    await pool.open()
    await pool.wait()
    await apply_migrations(pool, default_migrations_dir())
    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_audit_tables(app_pool) -> AsyncIterator[None]:
    async with app_pool.connection() as conn:
        await conn.execute(
            "TRUNCATE chat_sql_attempts, chat_audit_requests, chat_session_leases"
        )
    yield
    async with app_pool.connection() as conn:
        await conn.execute(
            "TRUNCATE chat_sql_attempts, chat_audit_requests, chat_session_leases"
        )


@pytest.fixture
def audit_repo(app_pool, settings: Settings) -> AuditRepository:
    return AuditRepository(app_pool, max_sql_attempts=settings.max_sql_attempts)
