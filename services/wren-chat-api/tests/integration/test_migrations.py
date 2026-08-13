"""Integration coverage for schema migrations and database constraints."""

from uuid import uuid4

import psycopg
import pytest

from wren_chat_api.db import (
    apply_migrations,
    create_app_pool,
    create_checkpoint_pool,
    default_migrations_dir,
)


async def test_migrations_are_idempotent_and_recorded(app_pool) -> None:
    await apply_migrations(app_pool, default_migrations_dir())
    await apply_migrations(app_pool, default_migrations_dir())

    async with app_pool.connection() as conn:
        result = await conn.execute(
            "SELECT version FROM wren_chat_schema_migrations ORDER BY version"
        )
        assert await result.fetchall() == [{"version": "0001_chat_audit"}]


async def test_pool_factories_keep_transaction_semantics_separate(settings) -> None:
    app_pool = create_app_pool(settings)
    checkpoint_pool = create_checkpoint_pool(settings)
    await app_pool.open()
    await checkpoint_pool.open()
    try:
        async with app_pool.connection() as app_conn:
            assert app_conn.autocommit is False
        async with checkpoint_pool.connection() as checkpoint_conn:
            assert checkpoint_conn.autocommit is True
            assert checkpoint_conn.prepare_threshold == 0
    finally:
        await app_pool.close()
        await checkpoint_pool.close()


async def test_running_attempt_must_not_have_result(app_pool, audit_repo) -> None:
    request_id = await audit_repo.start_request(
        session_id="session-1",
        thread_id="wren-chat:hash-1",
        question="Count orders",
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        async with app_pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO chat_sql_attempts (
                    attempt_id, request_id, sequence, semantic_sql, status,
                    row_limit, returned_row_count, result_truncated,
                    result, started_at
                )
                VALUES (
                    %s, %s, 1, 'SELECT 1', 'running', 100, 0, false,
                    '{"columns": [], "rows": []}', now()
                )
                """,
                (uuid4(), request_id),
            )


async def test_succeeded_request_requires_answer(app_pool) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        async with app_pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO chat_audit_requests (
                    request_id, session_id, thread_id, question,
                    status, started_at, completed_at
                )
                VALUES (%s, 's-1', 't-1', 'question', 'succeeded', now(), now())
                """,
                (uuid4(),),
            )


async def test_successful_attempt_requires_executed_sql(app_pool, audit_repo) -> None:
    request_id = await audit_repo.start_request(
        session_id="session-2",
        thread_id="wren-chat:hash-2",
        question="Count orders",
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        async with app_pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO chat_sql_attempts (
                    attempt_id, request_id, sequence, semantic_sql, status,
                    row_limit, returned_row_count, result_truncated,
                    result, started_at, completed_at, duration_ms
                )
                VALUES (
                    %s, %s, 1, 'SELECT 1', 'success', 100, 1, false,
                    '{"columns": ["value"], "rows": [{"value": 1}]}',
                    now(), now(), 0
                )
                """,
                (uuid4(), request_id),
            )
