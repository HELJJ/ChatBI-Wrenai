"""Pre-execution audited Wren query orchestration."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from wren_chat_api.audit import (
    AuditRepository,
    AuditRepositoryError,
    SqlAttemptLimitReached,
)
from wren_chat_api.config import Settings
from wren_chat_api.contracts import (
    AttemptError,
    AttemptResult,
    SessionId,
    StrictModel,
)
from wren_chat_api.errors import ChatServiceError, PersistenceFailed
from wren_chat_api.executor import BoundedWrenExecutor
from wren_chat_api.results import (
    build_attempt_result,
    build_tool_content,
    redact_secrets,
)
from wren_chat_api.sql_policy import validate_read_only_sql

_RETRY_EXHAUSTED_CODE = "SQL_RETRY_EXHAUSTED"
_GENERIC_FAILURE_CODE = "SQL_ATTEMPT_FAILED"


class RunContext(StrictModel):
    """Identity of one running chat request.

    Task 7 extends this with the audited query component itself when the
    agent graph threads runtime context through tool nodes.
    """

    request_id: UUID
    session_id: SessionId


class AuditedQuery:
    """Two-phase Wren execution with audit writes surrounding every step.

    The order is fixed: the running attempt is persisted first, planned SQL
    is persisted before execution, and every terminal outcome is recorded.
    Audit persistence failures stop the whole request; SQL failures are
    returned to the model as structured, correctable errors.
    """

    def __init__(
        self,
        *,
        audit: AuditRepository,
        toolkit: Any,
        settings: Settings,
        executor: Any | None = None,
        dialect: str = "postgres",
    ) -> None:
        self._audit = audit
        self._toolkit = toolkit
        self._settings = settings
        self._dialect = dialect
        self._executor = executor or BoundedWrenExecutor(
            workers=settings.wren_workers,
            queue_capacity=settings.wren_queue_capacity,
        )

    async def execute(
        self,
        context: RunContext,
        sql: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Run one audited read-only query and return a bounded envelope."""
        row_limit = max(1, min(limit, self._settings.max_row_limit))

        try:
            attempt = await self._audit.start_attempt(
                request_id=context.request_id,
                semantic_sql=sql,
                row_limit=row_limit,
            )
        except SqlAttemptLimitReached:
            return _error_envelope(
                AttemptError(
                    code=_RETRY_EXHAUSTED_CODE,
                    phase="SQL_RETRY",
                    message=(
                        "No further SQL attempts are permitted "
                        "for this request."
                    ),
                    metadata={
                        "attempt_limit": self._settings.max_sql_attempts,
                    },
                ),
            )
        except AuditRepositoryError as exc:
            raise PersistenceFailed("start_attempt", cause=exc) from exc

        phase = "SQL_PLANNING"
        try:
            validate_read_only_sql(sql, self._dialect)
            plan = await self._executor.run(self._toolkit.plan_query, sql)
            await self._persist(
                self._audit.set_executed_sql(
                    attempt_id=attempt.attempt_id,
                    executed_sql=plan.dialect_sql,
                ),
                "set_executed_sql",
            )
            phase = "SQL_EXECUTION"
            table = await self._executor.run(
                self._toolkit.execute_planned,
                plan,
                row_limit + 1,
            )
            normalized = build_attempt_result(
                table,
                row_limit=row_limit,
                max_bytes=self._settings.max_result_bytes,
            )
            await self._persist(
                self._audit.succeed_attempt(
                    attempt_id=attempt.attempt_id,
                    result=AttemptResult(
                        columns=normalized.result["columns"],
                        rows=normalized.result["rows"],
                    ),
                    returned_row_count=normalized.returned_row_count,
                    result_truncated=normalized.result_truncated,
                ),
                "succeed_attempt",
            )
        except asyncio.CancelledError:
            # The real planning/execution outcome is unknown; the recovery
            # worker later records SQL_ATTEMPT_INTERRUPTED.
            raise
        except PersistenceFailed:
            # Audit persistence failure must stop the request; it is never
            # downgraded to a model-visible, retryable SQL error.
            raise
        except Exception as exc:
            error = _structure_error(exc, phase=phase)
            await self._persist(
                self._audit.fail_attempt(
                    attempt_id=attempt.attempt_id,
                    error=error,
                ),
                "fail_attempt",
            )
            return _error_envelope(error)

        tool_content = build_tool_content(
            normalized,
            max_bytes=self._settings.max_tool_content_bytes,
        )
        return {
            "ok": True,
            "content": tool_content.content,
            "content_truncated": tool_content.content_truncated,
            "returned_row_count": normalized.returned_row_count,
            "result_truncated": normalized.result_truncated,
        }

    async def _persist(self, awaitable: Any, action: str) -> None:
        """Await one audit write, converting failure to PersistenceFailed."""
        try:
            await awaitable
        except AuditRepositoryError as exc:
            raise PersistenceFailed(action, cause=exc) from exc


def _structure_error(exc: Exception, *, phase: str) -> AttemptError:
    """Build a structured, redacted attempt error for the model."""
    if isinstance(exc, ChatServiceError):
        code = exc.code
        message = exc.public_message
    else:
        code = _GENERIC_FAILURE_CODE
        message = str(exc) or type(exc).__name__
    return AttemptError(
        code=code,
        phase=phase,
        message=message,
        metadata=redact_secrets({"outcome": "failed"}),
    )


def _error_envelope(error: AttemptError) -> dict[str, Any]:
    return {"ok": False, "error": error.model_dump(mode="json")}
