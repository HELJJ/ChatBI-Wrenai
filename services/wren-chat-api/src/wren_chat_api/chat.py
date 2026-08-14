"""Orchestration of one chat request across lease, audit, and graph."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from wren_chat_api.agent import invoke_chat
from wren_chat_api.audit import AuditRepository, RequestAlreadyTerminal
from wren_chat_api.audited_query import AuditedQuery, RunContext
from wren_chat_api.config import Settings
from wren_chat_api.contracts import (
    AttemptError,
    ChatRequest,
    ChatResponse,
)
from wren_chat_api.errors import (
    ChatServiceError,
    PersistenceFailed,
    SessionBusy,
    SessionLeaseLost,
)
from wren_chat_api.identity import derive_thread_id
from wren_chat_api.leases import Lease, LeaseRepository
from wren_chat_api.results import redact_secrets

logger = logging.getLogger(__name__)

_GENERIC_REQUEST_FAILURE_CODE = "REQUEST_FAILED"


class ChatService:
    """Run one session-isolated chat request to a terminal audit state.

    Order is fixed: acquire lease, insert running request, invoke the graph
    with a derived thread ID, mark the request terminal, release the lease.
    Any failure after ``start_request`` is persisted as a redacted
    structured request error before it is re-raised.
    """

    def __init__(
        self,
        *,
        leases: LeaseRepository,
        audit: AuditRepository,
        graph: Any,
        audited_query: AuditedQuery,
        settings: Settings,
    ) -> None:
        self._leases = leases
        self._audit = audit
        self._graph = graph
        self._audited_query = audited_query
        self._settings = settings

    async def ask(self, request: ChatRequest) -> ChatResponse:
        """Answer one question for one session, or raise a typed error."""
        lease = await self._leases.acquire(
            request.session_id,
            ttl=timedelta(seconds=self._settings.lease_ttl_seconds),
        )
        if lease is None:
            raise SessionBusy()

        try:
            return await self._ask_holding_lease(request, lease)
        finally:
            # Always attempt release, but a release error must never
            # overwrite an already-propagating primary failure.
            try:
                await self._leases.release(lease)
            except Exception:
                logger.warning(
                    "lease release failed for session %s",
                    request.session_id,
                    exc_info=True,
                )

    async def _ask_holding_lease(
        self,
        request: ChatRequest,
        lease: Lease,
    ) -> ChatResponse:
        thread_id = derive_thread_id(request.session_id)
        request_id = await self._guarded(
            self._audit.start_request(
                session_id=request.session_id,
                thread_id=thread_id,
                question=request.question,
            ),
            "start_request",
        )
        try:
            answer = await self._invoke_graph_with_renewal(
                request,
                request_id,
                lease,
                thread_id,
            )
        except Exception as exc:
            await self._fail_request_safely(request_id, exc)
            raise

        await self._guarded(
            self._audit.succeed_request(request_id=request_id, answer=answer),
            "succeed_request",
        )
        return ChatResponse(session_id=request.session_id, answer=answer)

    async def _invoke_graph_with_renewal(
        self,
        request: ChatRequest,
        request_id: UUID,
        lease: Lease,
        thread_id: str,
    ) -> str:
        """Run the graph while renewing the lease until the work is done."""
        graph_task = asyncio.create_task(
            invoke_chat(
                self._graph,
                thread_id,
                request.question,
                RunContext(
                    request_id=request_id,
                    session_id=request.session_id,
                    audited_query=self._audited_query,
                ),
                self._settings,
            )
        )
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {graph_task},
                    timeout=self._settings.lease_renew_seconds,
                )
                if done:
                    return graph_task.result()
                renewed = await self._leases.renew(
                    lease,
                    ttl=timedelta(seconds=self._settings.lease_ttl_seconds),
                )
                if not renewed:
                    raise SessionLeaseLost()
        finally:
            if not graph_task.done():
                graph_task.cancel()
                try:
                    await graph_task
                except asyncio.CancelledError:
                    logger.info(
                        "graph work cancelled after lease loss for %s",
                        request.session_id,
                    )

    async def _fail_request_safely(self, request_id: UUID, exc: Exception) -> None:
        """Persist a structured request failure, never masking the cause."""
        error = _structure_request_error(exc)
        try:
            await self._audit.fail_request(request_id=request_id, error=error)
        except RequestAlreadyTerminal:
            logger.warning(
                "request %s already terminal; failure not recorded",
                request_id,
            )
        except Exception as persist_exc:
            logger.critical(
                "failed to persist request failure for %s",
                request_id,
                exc_info=True,
            )
            raise PersistenceFailed(
                "fail_request",
                cause=persist_exc,
            ) from persist_exc

    async def _guarded(self, awaitable: Any, action: str) -> Any:
        """Await one audit write, converting failure to PersistenceFailed."""
        try:
            return await awaitable
        except Exception as exc:
            raise PersistenceFailed(action, cause=exc) from exc


def _structure_request_error(exc: Exception) -> AttemptError:
    """Build a redacted, structured request error for the audit store."""
    if isinstance(exc, ChatServiceError):
        code = exc.code
        message = exc.public_message
    else:
        code = _GENERIC_REQUEST_FAILURE_CODE
        message = str(exc) or type(exc).__name__
    return AttemptError(
        code=code,
        phase="REQUEST",
        message=message,
        metadata=redact_secrets({"outcome": "failed"}),
    )
