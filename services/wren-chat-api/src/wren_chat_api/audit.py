"""Incremental PostgreSQL audit persistence for chat requests and SQL attempts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from wren_chat_api.contracts import (
    Answer,
    AttemptError,
    AttemptResult,
    Question,
    SessionId,
    StrictModel,
)


class AuditRepositoryError(RuntimeError):
    """Base class for invalid or missing audit state transitions."""


class AuditRequestNotFound(AuditRepositoryError):
    """Raised when the requested audit request does not exist."""


class RequestAlreadyTerminal(AuditRepositoryError):
    """Raised when a request is missing or no longer running."""


class AttemptAlreadyTerminal(AuditRepositoryError):
    """Raised when an attempt is missing or no longer running."""


class SqlAttemptLimitReached(AuditRepositoryError):
    """Raised before inserting an attempt beyond the configured maximum."""


class StartedAttempt(StrictModel):
    """Identity and ordering assigned when a running attempt is persisted."""

    attempt_id: UUID
    sequence: int = Field(ge=1)


class CanonicalSqlAttempt(StrictModel):
    """Business-facing internal view of one SQL attempt."""

    sequence: int = Field(ge=1)
    semantic_sql: str
    executed_sql: str | None
    status: Literal["running", "success", "failed"]
    row_limit: int = Field(ge=1)
    returned_row_count: int = Field(ge=0)
    result_truncated: bool
    result: AttemptResult | None
    error: AttemptError | None


class CanonicalAudit(StrictModel):
    """Internal canonical audit view ordered by SQL attempt sequence."""

    session_id: SessionId
    question: Question
    answer: Answer | None
    sql_attempts: list[CanonicalSqlAttempt]


class AuditRepository:
    """Persist short, conditional audit state transitions in PostgreSQL."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        max_sql_attempts: int,
    ) -> None:
        if not 1 <= max_sql_attempts <= 3:
            raise ValueError("max_sql_attempts must be between 1 and 3")
        self.pool = pool
        self.max_sql_attempts = max_sql_attempts

    async def start_request(
        self,
        *,
        session_id: str,
        thread_id: str,
        question: str,
        request_id: UUID | None = None,
    ) -> UUID:
        """Insert a running request before model or database work begins."""
        actual_request_id = request_id or uuid4()
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO chat_audit_requests (
                    request_id, session_id, thread_id, question,
                    status, started_at
                )
                VALUES (%s, %s, %s, %s, 'running', clock_timestamp())
                """,
                (actual_request_id, session_id, thread_id, question),
            )
        return actual_request_id

    async def succeed_request(self, *, request_id: UUID, answer: str) -> None:
        """Finalize a running request with its natural-language answer."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE chat_audit_requests
                SET status = 'succeeded',
                    answer = %s,
                    completed_at = clock_timestamp()
                WHERE request_id = %s
                  AND status = 'running'
                """,
                (answer, request_id),
            )
            if cursor.rowcount != 1:
                raise RequestAlreadyTerminal(str(request_id))

    async def fail_request(
        self,
        *,
        request_id: UUID,
        error: AttemptError,
    ) -> None:
        """Finalize a running request with a structured internal error."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE chat_audit_requests
                SET status = 'failed',
                    error = %s,
                    completed_at = clock_timestamp()
                WHERE request_id = %s
                  AND status = 'running'
                """,
                (Jsonb(error.model_dump(mode="json")), request_id),
            )
            if cursor.rowcount != 1:
                raise RequestAlreadyTerminal(str(request_id))

    async def start_attempt(
        self,
        *,
        request_id: UUID,
        semantic_sql: str,
        row_limit: int,
        attempt_id: UUID | None = None,
    ) -> StartedAttempt:
        """Allocate and insert the next running attempt in one transaction."""
        actual_attempt_id = attempt_id or uuid4()
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    """
                    SELECT status
                    FROM chat_audit_requests
                    WHERE request_id = %s
                    FOR UPDATE
                    """,
                    (request_id,),
                )
                request = await cursor.fetchone()
                if request is None:
                    raise AuditRequestNotFound(str(request_id))
                if request["status"] != "running":
                    raise RequestAlreadyTerminal(str(request_id))

                cursor = await conn.execute(
                    """
                    SELECT COUNT(*) AS attempt_count,
                           COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                    FROM chat_sql_attempts
                    WHERE request_id = %s
                    """,
                    (request_id,),
                )
                state = await cursor.fetchone()
                if state["attempt_count"] >= self.max_sql_attempts:
                    raise SqlAttemptLimitReached(str(request_id))

                sequence = state["next_sequence"]
                await conn.execute(
                    """
                    INSERT INTO chat_sql_attempts (
                        attempt_id, request_id, sequence, semantic_sql,
                        status, row_limit, returned_row_count,
                        result_truncated, started_at
                    )
                    VALUES (%s, %s, %s, %s, 'running', %s, 0, false,
                            clock_timestamp())
                    """,
                    (
                        actual_attempt_id,
                        request_id,
                        sequence,
                        semantic_sql,
                        row_limit,
                    ),
                )

        return StartedAttempt(
            attempt_id=actual_attempt_id,
            sequence=sequence,
        )

    async def set_executed_sql(
        self,
        *,
        attempt_id: UUID,
        executed_sql: str,
    ) -> None:
        """Persist planned target SQL before connector execution starts."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE chat_sql_attempts
                SET executed_sql = %s
                WHERE attempt_id = %s
                  AND status = 'running'
                """,
                (executed_sql, attempt_id),
            )
            if cursor.rowcount != 1:
                raise AttemptAlreadyTerminal(str(attempt_id))

    async def succeed_attempt(
        self,
        *,
        attempt_id: UUID,
        result: AttemptResult,
        returned_row_count: int,
        result_truncated: bool,
    ) -> None:
        """Atomically finalize a running attempt with its retained result."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE chat_sql_attempts
                SET status = 'success',
                    returned_row_count = %s,
                    result_truncated = %s,
                    result = %s,
                    completed_at = clock_timestamp(),
                    duration_ms = GREATEST(
                        0,
                        FLOOR(EXTRACT(EPOCH FROM (
                            clock_timestamp() - started_at
                        )) * 1000)::INTEGER
                    )
                WHERE attempt_id = %s
                  AND status = 'running'
                """,
                (
                    returned_row_count,
                    result_truncated,
                    Jsonb(result.model_dump(mode="json")),
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise AttemptAlreadyTerminal(str(attempt_id))

    async def fail_attempt(
        self,
        *,
        attempt_id: UUID,
        error: AttemptError,
    ) -> None:
        """Atomically finalize a running attempt with a structured error."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE chat_sql_attempts
                SET status = 'failed',
                    error = %s,
                    completed_at = clock_timestamp(),
                    duration_ms = GREATEST(
                        0,
                        FLOOR(EXTRACT(EPOCH FROM (
                            clock_timestamp() - started_at
                        )) * 1000)::INTEGER
                    )
                WHERE attempt_id = %s
                  AND status = 'running'
                """,
                (Jsonb(error.model_dump(mode="json")), attempt_id),
            )
            if cursor.rowcount != 1:
                raise AttemptAlreadyTerminal(str(attempt_id))

    async def get_canonical_audit(self, request_id: UUID) -> CanonicalAudit:
        """Read one request and all of its attempts in execution order."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT session_id, question, answer
                FROM chat_audit_requests
                WHERE request_id = %s
                """,
                (request_id,),
            )
            request = await cursor.fetchone()
            if request is None:
                raise AuditRequestNotFound(str(request_id))

            cursor = await conn.execute(
                """
                SELECT sequence, semantic_sql, executed_sql, status,
                       row_limit, returned_row_count, result_truncated,
                       result, error
                FROM chat_sql_attempts
                WHERE request_id = %s
                ORDER BY sequence ASC
                """,
                (request_id,),
            )
            attempts = await cursor.fetchall()

        return CanonicalAudit(
            session_id=request["session_id"],
            question=request["question"],
            answer=request["answer"],
            sql_attempts=[CanonicalSqlAttempt.model_validate(row) for row in attempts],
        )
