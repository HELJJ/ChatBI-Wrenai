"""PostgreSQL integration coverage for interrupted request recovery."""

from datetime import datetime, timedelta, timezone

from wren_chat_api.recovery import (
    RecoveryCounts,
    recover_interrupted,
)


async def make_stale_attempt(
    app_pool,
    audit_repo,
    *,
    session_id: str,
    executed_sql: str | None,
):
    request_id = await audit_repo.start_request(
        session_id=session_id,
        thread_id=f"wren-chat:{session_id}",
        question="Count orders",
    )
    attempt = await audit_repo.start_attempt(
        request_id=request_id,
        semantic_sql="SELECT COUNT(*) FROM orders",
        row_limit=100,
    )
    if executed_sql is not None:
        await audit_repo.set_executed_sql(
            attempt_id=attempt.attempt_id,
            executed_sql=executed_sql,
        )
    async with app_pool.connection() as conn:
        await conn.execute(
            """
            UPDATE chat_audit_requests
            SET started_at = clock_timestamp() - interval '10 minutes'
            WHERE request_id = %s
            """,
            (request_id,),
        )
        await conn.execute(
            """
            UPDATE chat_sql_attempts
            SET started_at = clock_timestamp() - interval '10 minutes'
            WHERE attempt_id = %s
            """,
            (attempt.attempt_id,),
        )
    return request_id, attempt.attempt_id


async def fetch_states(app_pool, request_id, attempt_id):
    async with app_pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT status, error, completed_at
            FROM chat_audit_requests
            WHERE request_id = %s
            """,
            (request_id,),
        )
        request = await cursor.fetchone()
        cursor = await conn.execute(
            """
            SELECT status, semantic_sql, executed_sql, result, error,
                   completed_at, duration_ms
            FROM chat_sql_attempts
            WHERE attempt_id = %s
            """,
            (attempt_id,),
        )
        attempt = await cursor.fetchone()
    return request, attempt


async def test_recovery_marks_planning_and_execution_interruptions(
    app_pool, audit_repo
) -> None:
    planning_request, planning_attempt = await make_stale_attempt(
        app_pool,
        audit_repo,
        session_id="planning-session",
        executed_sql=None,
    )
    execution_request, execution_attempt = await make_stale_attempt(
        app_pool,
        audit_repo,
        session_id="execution-session",
        executed_sql="SELECT COUNT(*) FROM physical_orders",
    )
    recovery_time = datetime.now(timezone.utc)

    counts = await recover_interrupted(
        app_pool,
        now=recovery_time,
        threshold=timedelta(seconds=150),
    )

    assert counts == RecoveryCounts(attempts_recovered=2, requests_recovered=2)
    planning_parent, planning = await fetch_states(
        app_pool, planning_request, planning_attempt
    )
    execution_parent, execution = await fetch_states(
        app_pool, execution_request, execution_attempt
    )
    assert planning["status"] == "failed"
    assert planning["executed_sql"] is None
    assert planning["result"] is None
    assert planning["error"]["code"] == "SQL_ATTEMPT_INTERRUPTED"
    assert planning["error"]["phase"] == "SQL_PLANNING"
    assert planning["error"]["metadata"] == {"outcome": "unknown"}
    assert execution["status"] == "failed"
    assert execution["executed_sql"] == "SELECT COUNT(*) FROM physical_orders"
    assert execution["error"]["phase"] == "SQL_EXECUTION"
    assert execution["error"]["metadata"] == {"outcome": "unknown"}
    assert planning["completed_at"] == recovery_time
    assert execution["completed_at"] == recovery_time
    assert planning["duration_ms"] >= 0
    assert execution["duration_ms"] >= 0
    assert planning_parent["status"] == "failed"
    assert execution_parent["status"] == "failed"
    assert planning_parent["error"]["code"] == "REQUEST_INTERRUPTED"
    assert execution_parent["error"]["code"] == "REQUEST_INTERRUPTED"


async def test_live_lease_protects_stale_request(app_pool, audit_repo, leases) -> None:
    request_id, attempt_id = await make_stale_attempt(
        app_pool,
        audit_repo,
        session_id="live-session",
        executed_sql=None,
    )
    lease = await leases.acquire("live-session", ttl=timedelta(seconds=30))
    assert lease is not None

    counts = await recover_interrupted(
        app_pool,
        now=datetime.now(timezone.utc),
        threshold=timedelta(seconds=150),
    )

    assert counts == RecoveryCounts(attempts_recovered=0, requests_recovered=0)
    request, attempt = await fetch_states(app_pool, request_id, attempt_id)
    assert request["status"] == "running"
    assert attempt["status"] == "running"


async def test_recovery_is_idempotent(app_pool, audit_repo) -> None:
    await make_stale_attempt(
        app_pool,
        audit_repo,
        session_id="idempotent-session",
        executed_sql=None,
    )
    recovery_time = datetime.now(timezone.utc)

    first = await recover_interrupted(
        app_pool,
        now=recovery_time,
        threshold=timedelta(seconds=150),
    )
    second = await recover_interrupted(
        app_pool,
        now=recovery_time,
        threshold=timedelta(seconds=150),
    )

    assert first == RecoveryCounts(attempts_recovered=1, requests_recovered=1)
    assert second == RecoveryCounts(attempts_recovered=0, requests_recovered=0)


async def test_recent_running_attempt_keeps_parent_running(
    app_pool, audit_repo
) -> None:
    request_id = await audit_repo.start_request(
        session_id="recent-session",
        thread_id="wren-chat:recent",
        question="Count orders",
    )
    attempt = await audit_repo.start_attempt(
        request_id=request_id,
        semantic_sql="SELECT COUNT(*) FROM orders",
        row_limit=100,
    )
    async with app_pool.connection() as conn:
        await conn.execute(
            """
            UPDATE chat_audit_requests
            SET started_at = clock_timestamp() - interval '10 minutes'
            WHERE request_id = %s
            """,
            (request_id,),
        )

    counts = await recover_interrupted(
        app_pool,
        now=datetime.now(timezone.utc),
        threshold=timedelta(seconds=150),
    )

    assert counts == RecoveryCounts(attempts_recovered=0, requests_recovered=0)
    request, current_attempt = await fetch_states(
        app_pool, request_id, attempt.attempt_id
    )
    assert request["status"] == "running"
    assert current_attempt["status"] == "running"


async def test_stale_request_without_attempts_is_recovered(
    app_pool, audit_repo
) -> None:
    request_id = await audit_repo.start_request(
        session_id="no-attempt-session",
        thread_id="wren-chat:no-attempt",
        question="Hello?",
    )
    async with app_pool.connection() as conn:
        await conn.execute(
            """
            UPDATE chat_audit_requests
            SET started_at = clock_timestamp() - interval '10 minutes'
            WHERE request_id = %s
            """,
            (request_id,),
        )

    counts = await recover_interrupted(
        app_pool,
        now=datetime.now(timezone.utc),
        threshold=timedelta(seconds=150),
    )

    assert counts == RecoveryCounts(attempts_recovered=0, requests_recovered=1)
    async with app_pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT status, error, completed_at
            FROM chat_audit_requests
            WHERE request_id = %s
            """,
            (request_id,),
        )
        request = await cursor.fetchone()
    assert request["status"] == "failed"
    assert request["error"]["code"] == "REQUEST_INTERRUPTED"
    assert request["completed_at"] is not None


async def test_very_old_attempt_duration_is_capped(app_pool, audit_repo) -> None:
    request_id, attempt_id = await make_stale_attempt(
        app_pool,
        audit_repo,
        session_id="very-old-session",
        executed_sql=None,
    )
    async with app_pool.connection() as conn:
        await conn.execute(
            """
            UPDATE chat_audit_requests
            SET started_at = clock_timestamp() - interval '30 days'
            WHERE request_id = %s
            """,
            (request_id,),
        )
        await conn.execute(
            """
            UPDATE chat_sql_attempts
            SET started_at = clock_timestamp() - interval '30 days'
            WHERE attempt_id = %s
            """,
            (attempt_id,),
        )

    counts = await recover_interrupted(
        app_pool,
        now=datetime.now(timezone.utc),
        threshold=timedelta(seconds=150),
    )

    assert counts == RecoveryCounts(attempts_recovered=1, requests_recovered=1)
    _, attempt = await fetch_states(app_pool, request_id, attempt_id)
    assert attempt["duration_ms"] == 2_147_483_647
