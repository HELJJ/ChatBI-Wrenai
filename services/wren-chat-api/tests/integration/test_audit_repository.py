"""Integration coverage for incremental audit persistence."""

from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from wren_chat_api.audit import (
    AttemptAlreadyTerminal,
    RequestAlreadyTerminal,
    SqlAttemptLimitReached,
)
from wren_chat_api.contracts import AttemptError, AttemptResult


def planning_error(message: str = "Unknown column") -> AttemptError:
    return AttemptError(
        code="INVALID_SQL",
        phase="SQL_PLANNING",
        message=message,
        metadata={},
    )


async def test_failed_then_corrected_sql_is_returned_in_sequence(
    audit_repo,
) -> None:
    request_id = await audit_repo.start_request(
        session_id="session-1",
        thread_id="wren-chat:hash-1",
        question="What are total sales?",
    )

    first = await audit_repo.start_attempt(
        request_id=request_id,
        semantic_sql="SELECT SUM(sales_amount) FROM orders",
        row_limit=100,
    )
    await audit_repo.fail_attempt(
        attempt_id=first.attempt_id,
        error=planning_error(),
    )

    second = await audit_repo.start_attempt(
        request_id=request_id,
        semantic_sql="SELECT SUM(amount) AS total_sales FROM orders",
        row_limit=100,
    )
    executed_sql = "WITH orders AS (...) SELECT SUM(amount) AS total_sales"
    await audit_repo.set_executed_sql(
        attempt_id=second.attempt_id,
        executed_sql=executed_sql,
    )
    await audit_repo.succeed_attempt(
        attempt_id=second.attempt_id,
        result=AttemptResult(
            columns=["total_sales"],
            rows=[{"total_sales": "1280000.00"}],
        ),
        returned_row_count=1,
        result_truncated=False,
    )
    await audit_repo.succeed_request(
        request_id=request_id,
        answer="Total sales were 1.28 million.",
    )

    audit = await audit_repo.get_canonical_audit(request_id)

    assert audit.session_id == "session-1"
    assert audit.question == "What are total sales?"
    assert audit.answer == "Total sales were 1.28 million."
    assert [item.sequence for item in audit.sql_attempts] == [1, 2]
    assert audit.sql_attempts[0].status == "failed"
    assert audit.sql_attempts[0].executed_sql is None
    assert audit.sql_attempts[0].error == planning_error()
    assert audit.sql_attempts[1].status == "success"
    assert audit.sql_attempts[1].executed_sql == executed_sql
    assert audit.sql_attempts[1].returned_row_count == 1
    assert audit.sql_attempts[1].result == AttemptResult(
        columns=["total_sales"],
        rows=[{"total_sales": "1280000.00"}],
    )


async def test_attempt_is_inserted_running_before_terminal_update(audit_repo) -> None:
    request_id = await audit_repo.start_request(
        session_id="session-running",
        thread_id="wren-chat:running",
        question="Count orders",
    )

    started = await audit_repo.start_attempt(
        request_id=request_id,
        semantic_sql="SELECT COUNT(*) FROM orders",
        row_limit=100,
    )
    audit = await audit_repo.get_canonical_audit(request_id)

    assert started.sequence == 1
    assert audit.sql_attempts[0].status == "running"
    assert audit.sql_attempts[0].executed_sql is None
    assert audit.sql_attempts[0].result is None
    assert audit.sql_attempts[0].error is None


async def test_attempts_are_read_by_sequence_not_insertion_order(
    app_pool, audit_repo
) -> None:
    request_id = await audit_repo.start_request(
        session_id="session-order",
        thread_id="wren-chat:order",
        question="Compare totals",
    )
    result = Jsonb({"columns": ["value"], "rows": [{"value": 1}]})

    async with app_pool.connection() as conn:
        for sequence in (2, 1):
            await conn.execute(
                """
                INSERT INTO chat_sql_attempts (
                    attempt_id, request_id, sequence, semantic_sql,
                    executed_sql, status, row_limit, returned_row_count,
                    result_truncated, result, started_at, completed_at,
                    duration_ms
                )
                VALUES (
                    %s, %s, %s, %s, %s, 'success', 100, 1, false,
                    %s, clock_timestamp(), clock_timestamp(), 0
                )
                """,
                (
                    uuid4(),
                    request_id,
                    sequence,
                    f"SELECT {sequence}",
                    f"SELECT {sequence}",
                    result,
                ),
            )

    audit = await audit_repo.get_canonical_audit(request_id)

    assert [attempt.sequence for attempt in audit.sql_attempts] == [1, 2]


async def test_terminal_attempt_cannot_be_overwritten(audit_repo) -> None:
    request_id = await audit_repo.start_request(
        session_id="session-terminal",
        thread_id="wren-chat:terminal",
        question="Count orders",
    )
    attempt = await audit_repo.start_attempt(
        request_id=request_id,
        semantic_sql="SELECT missing FROM orders",
        row_limit=100,
    )
    await audit_repo.fail_attempt(
        attempt_id=attempt.attempt_id,
        error=planning_error(),
    )

    with pytest.raises(AttemptAlreadyTerminal):
        await audit_repo.set_executed_sql(
            attempt_id=attempt.attempt_id,
            executed_sql="SELECT missing FROM orders",
        )

    with pytest.raises(AttemptAlreadyTerminal):
        await audit_repo.succeed_attempt(
            attempt_id=attempt.attempt_id,
            result=AttemptResult(columns=[], rows=[]),
            returned_row_count=0,
            result_truncated=False,
        )


async def test_fourth_attempt_is_rejected(audit_repo) -> None:
    request_id = await audit_repo.start_request(
        session_id="session-limit",
        thread_id="wren-chat:limit",
        question="Count orders",
    )

    for sequence in range(1, 4):
        attempt = await audit_repo.start_attempt(
            request_id=request_id,
            semantic_sql=f"SELECT {sequence}",
            row_limit=100,
        )
        await audit_repo.fail_attempt(
            attempt_id=attempt.attempt_id,
            error=planning_error(f"failure {sequence}"),
        )

    with pytest.raises(SqlAttemptLimitReached):
        await audit_repo.start_attempt(
            request_id=request_id,
            semantic_sql="SELECT 4",
            row_limit=100,
        )


async def test_terminal_request_cannot_be_overwritten(audit_repo) -> None:
    request_id = await audit_repo.start_request(
        request_id=uuid4(),
        session_id="session-request-terminal",
        thread_id="wren-chat:request-terminal",
        question="Count orders",
    )
    await audit_repo.fail_request(
        request_id=request_id,
        error=AttemptError(
            code="REQUEST_INTERRUPTED",
            phase="REQUEST",
            message="Request was interrupted",
            metadata={},
        ),
    )

    with pytest.raises(RequestAlreadyTerminal):
        await audit_repo.succeed_request(request_id=request_id, answer="42")
