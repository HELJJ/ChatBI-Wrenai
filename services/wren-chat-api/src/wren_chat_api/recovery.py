"""Recovery of stale chat requests whose session ownership has expired."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from wren_chat_api.contracts import StrictModel

_ATTEMPT_INTERRUPTED_MESSAGE = (
    "The service stopped before the SQL attempt outcome was recorded."
)
_REQUEST_INTERRUPTED_MESSAGE = (
    "The service stopped before the request outcome was recorded."
)


class RecoveryCounts(StrictModel):
    """Numbers of stale attempt and request rows terminalized in one run."""

    attempts_recovered: int = Field(ge=0)
    requests_recovered: int = Field(ge=0)


async def recover_interrupted(
    pool: AsyncConnectionPool,
    *,
    now: datetime,
    threshold: timedelta,
) -> RecoveryCounts:
    """Fail stale running work with no live session lease, idempotently."""
    recovery_time = _require_aware_utc(now)
    if threshold <= timedelta(0):
        raise ValueError("threshold must be positive")
    cutoff = recovery_time - threshold

    async with pool.connection() as conn:
        async with conn.transaction():
            attempt_cursor = await conn.execute(
                """
                UPDATE chat_sql_attempts AS attempt
                SET status = 'failed',
                    error = jsonb_build_object(
                        'code', 'SQL_ATTEMPT_INTERRUPTED',
                        'phase', CASE
                            WHEN attempt.executed_sql IS NULL
                                THEN 'SQL_PLANNING'
                            ELSE 'SQL_EXECUTION'
                        END,
                        'message', %s::text,
                        'metadata', jsonb_build_object('outcome', 'unknown')
                    ),
                    completed_at = %s,
                    duration_ms = LEAST(
                        2147483647,
                        GREATEST(
                            0,
                            FLOOR(EXTRACT(EPOCH FROM (%s - attempt.started_at))
                                  * 1000)
                        )
                    )::INTEGER
                FROM chat_audit_requests AS request
                WHERE attempt.request_id = request.request_id
                  AND attempt.status = 'running'
                  AND request.status = 'running'
                  AND attempt.started_at <= %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM chat_session_leases AS lease
                      WHERE lease.session_id = request.session_id
                        AND lease.expires_at > %s
                  )
                RETURNING attempt.request_id
                """,
                (
                    _ATTEMPT_INTERRUPTED_MESSAGE,
                    recovery_time,
                    recovery_time,
                    cutoff,
                    recovery_time,
                ),
            )
            recovered_request_rows = await attempt_cursor.fetchall()
            recovered_request_ids = [
                row["request_id"] for row in recovered_request_rows
            ]
            request_cursor = await conn.execute(
                """
                UPDATE chat_audit_requests AS request
                SET status = 'failed',
                    error = jsonb_build_object(
                        'code', 'REQUEST_INTERRUPTED',
                        'phase', 'REQUEST',
                        'message', %s::text,
                        'metadata', jsonb_build_object()
                    ),
                    completed_at = %s
                WHERE request.status = 'running'
                  AND (
                      request.request_id = ANY(%s::uuid[])
                      OR (
                          request.started_at <= %s
                          AND NOT EXISTS (
                              SELECT 1
                              FROM chat_session_leases AS lease
                              WHERE lease.session_id = request.session_id
                                AND lease.expires_at > %s
                          )
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM chat_sql_attempts AS active_attempt
                      WHERE active_attempt.request_id = request.request_id
                        AND active_attempt.status = 'running'
                  )
                RETURNING request.request_id
                """,
                (
                    _REQUEST_INTERRUPTED_MESSAGE,
                    recovery_time,
                    recovered_request_ids,
                    cutoff,
                    recovery_time,
                ),
            )
            requests_recovered = len(await request_cursor.fetchall())

    return RecoveryCounts(
        attempts_recovered=len(recovered_request_rows),
        requests_recovered=requests_recovered,
    )


async def run_recovery_loop(
    *,
    stop_event: asyncio.Event,
    pool: AsyncConnectionPool,
    interval_seconds: int,
    threshold_seconds: int,
) -> None:
    """Recover immediately and then repeat until shutdown is requested."""
    if interval_seconds < 1:
        raise ValueError("interval_seconds must be positive")
    if threshold_seconds < 1:
        raise ValueError("threshold_seconds must be positive")

    while not stop_event.is_set():
        await recover_interrupted(
            pool,
            now=datetime.now(timezone.utc),
            threshold=timedelta(seconds=threshold_seconds),
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)
