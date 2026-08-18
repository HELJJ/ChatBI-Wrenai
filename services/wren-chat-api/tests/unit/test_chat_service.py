"""Unit contracts for chat request orchestration."""

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError
from openai import APIConnectionError
from psycopg import OperationalError

from wren_chat_api import errors as errors_module
from wren_chat_api.chat import ChatService
from wren_chat_api.config import Settings
from wren_chat_api.contracts import ChatRequest
from wren_chat_api.errors import (
    ChatServiceError,
    PersistenceFailed,
    RequestTimedOut,
    SessionBusy,
    SessionLeaseLost,
    UpstreamFailed,
)
from wren_chat_api.leases import Lease


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "state_database_url": "postgresql://user:pass@localhost:5432/wren_test",
        "api_key": "integration-test-key",
        "project_path": tmp_path,
        "model": "test-model",
        "lease_renew_seconds": 1,
    }
    values.update(overrides)
    return Settings(**values)


_UNSET = object()


class FakeLeases:
    def __init__(
        self,
        events,
        *,
        lease: Lease | None | object = _UNSET,
        renew_result: bool = True,
    ) -> None:
        self.events = events
        # lease=None means "acquisition fails (busy)"; the sentinel keeps
        # that distinct from "caller did not specify a lease".
        if lease is _UNSET:
            lease = Lease(
                session_id="s-1",
                lease_id=uuid4(),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            )
        self.lease = lease  # type: ignore[assignment]
        self.renew_result = renew_result

    async def acquire(self, session_id, *, ttl):
        self.events.append("lease:acquire")
        return self.lease

    async def renew(self, lease, *, ttl):
        self.events.append("lease:renew")
        return self.renew_result

    async def release(self, lease):
        self.events.append("lease:release")
        return True


class FakeAudit:
    def __init__(
        self,
        events,
        *,
        succeed_error: Exception | None = None,
        fail_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.succeed_error = succeed_error
        self.fail_error = fail_error
        self.start_calls = 0
        self.request_id = uuid4()
        self.failed_errors: list[dict] = []

    async def start_request(self, *, session_id, thread_id, question):
        self.events.append("audit:start_request")
        self.start_calls += 1
        return self.request_id

    async def succeed_request(self, *, request_id, answer):
        self.events.append("audit:succeed_request")
        if self.succeed_error is not None:
            raise self.succeed_error

    async def fail_request(self, *, request_id, error):
        self.events.append("audit:fail_request")
        if self.fail_error is not None:
            raise self.fail_error
        self.failed_errors.append(error.model_dump())


class FakeGraph:
    def __init__(self, events, *, answer: str = "42", hang: bool = False) -> None:
        self.events = events
        self.answer = answer
        self.hang = hang

    async def ainvoke(self, inputs, config=None, context=None, durability=None):
        self.events.append("graph:invoke")
        try:
            if self.hang:
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.events.append("graph:cancelled")
            raise
        return {
            "messages": [
                HumanMessage(content="q"),
                AIMessage(content=self.answer),
            ]
        }


def make_service(
    events,
    settings,
    *,
    leases=None,
    audit=None,
    graph=None,
):
    service = ChatService(
        leases=leases if leases is not None else FakeLeases(events),
        audit=audit if audit is not None else FakeAudit(events),
        graph=graph if graph is not None else FakeGraph(events),
        audited_query=object(),
        settings=settings,
    )
    return service


def make_request() -> ChatRequest:
    return ChatRequest(session_id="s-1", question="count")


async def test_success_is_returned_only_after_audit_is_terminal(tmp_path):
    events: list[str] = []
    service = make_service(events, make_settings(tmp_path))

    response = await service.ask(make_request())

    assert response.model_dump() == {"session_id": "s-1", "answer": "42"}
    assert events.index("audit:succeed_request") < events.index("lease:release")
    assert events == [
        "lease:acquire",
        "audit:start_request",
        "graph:invoke",
        "audit:succeed_request",
        "lease:release",
    ]


async def test_audit_failure_prevents_success_response(tmp_path):
    events: list[str] = []
    audit = FakeAudit(events, succeed_error=RuntimeError("postgres down"))
    service = make_service(events, make_settings(tmp_path), audit=audit)

    with pytest.raises(PersistenceFailed):
        await service.ask(make_request())

    assert "audit:succeed_request" in events
    assert events.index("lease:release") == len(events) - 1


async def test_busy_session_does_not_create_audit_request(tmp_path):
    events: list[str] = []
    leases = FakeLeases(events, lease=None)
    audit = FakeAudit(events)
    service = make_service(events, make_settings(tmp_path), leases=leases, audit=audit)

    with pytest.raises(SessionBusy):
        await service.ask(make_request())

    assert audit.start_calls == 0
    assert "lease:release" not in events


async def test_graph_failure_is_recorded_and_re_raised(tmp_path):
    events: list[str] = []
    graph = FakeGraph(events)
    graph.ainvoke = _raising_invoke(RuntimeError("model exploded"))
    audit = FakeAudit(events)
    service = make_service(events, make_settings(tmp_path), audit=audit, graph=graph)

    with pytest.raises(RuntimeError, match="model exploded"):
        await service.ask(make_request())

    assert len(audit.failed_errors) == 1
    assert audit.failed_errors[0]["code"] == "REQUEST_FAILED"
    assert audit.failed_errors[0]["phase"] == "REQUEST"
    assert events.index("audit:fail_request") < events.index("lease:release")


async def test_failure_persistence_failure_raises_persistence_failed(tmp_path):
    events: list[str] = []
    graph = FakeGraph(events)
    graph.ainvoke = _raising_invoke(RuntimeError("model exploded"))
    audit = FakeAudit(events, fail_error=RuntimeError("audit also down"))
    service = make_service(events, make_settings(tmp_path), audit=audit, graph=graph)

    with pytest.raises(PersistenceFailed):
        await service.ask(make_request())


async def test_lease_loss_cancels_graph_and_fails_request(tmp_path):
    events: list[str] = []
    leases = FakeLeases(events, renew_result=False)
    graph = FakeGraph(events, hang=True)
    audit = FakeAudit(events)
    service = make_service(
        events,
        make_settings(tmp_path, lease_renew_seconds=1),
        leases=leases,
        audit=audit,
        graph=graph,
    )

    with pytest.raises(SessionLeaseLost):
        await service.ask(make_request())

    assert "graph:cancelled" in events
    assert len(audit.failed_errors) == 1
    assert audit.failed_errors[0]["code"] == "SESSION_LEASE_LOST"
    assert events.index("graph:cancelled") < events.index("audit:fail_request")
    assert events.index("audit:fail_request") < events.index("lease:release")


def _raising_invoke(error: Exception):
    async def ainvoke(inputs, config=None, context=None, durability=None):
        raise error

    return ainvoke


async def test_recursion_limit_returns_graceful_answer_not_500(tmp_path):
    """GraphRecursionError must degrade to a normal 200 answer with retry
    guidance (audited as succeeded), not surface as INTERNAL_ERROR. Field
    failure kylin-006: 12-step budget exhausted before the final turn."""
    events: list[str] = []
    graph = FakeGraph(events)
    graph.ainvoke = _raising_invoke(
        GraphRecursionError("Recursion limit of 300 reached")
    )
    audit = FakeAudit(events)
    service = make_service(events, make_settings(tmp_path), audit=audit, graph=graph)

    response = await service.ask(make_request())

    assert response.session_id == "s-1"
    assert "缩小" in response.answer
    assert audit.failed_errors == []
    # _raising_invoke replaces FakeGraph.ainvoke, so no "graph:invoke" event.
    assert events == [
        "lease:acquire",
        "audit:start_request",
        "audit:succeed_request",
        "lease:release",
    ]


async def test_context_overflow_returns_graceful_answer_with_new_session_hint(
    tmp_path,
):
    """A model-endpoint context-window rejection (CMCC MaaS 424 phrasing)
    must degrade to a 200 answer whose guidance includes starting a fresh
    session, not a 500 INTERNAL_ERROR."""
    events: list[str] = []
    graph = FakeGraph(events)
    graph.ainvoke = _raising_invoke(
        RuntimeError(
            "Error code: 424 - input ids length cannot be greater than "
            "maxPositionEmbeddings"
        )
    )
    audit = FakeAudit(events)
    service = make_service(events, make_settings(tmp_path), audit=audit, graph=graph)

    response = await service.ask(make_request())

    assert response.session_id == "s-1"
    assert "新的会话" in response.answer
    assert audit.failed_errors == []
    assert events == [
        "lease:acquire",
        "audit:start_request",
        "audit:succeed_request",
        "lease:release",
    ]


async def test_request_timeout_maps_to_504_typed_error(tmp_path):
    """asyncio.timeout's TimeoutError must surface as REQUEST_TIMED_OUT (504)
    with guidance, not the generic 500 INTERNAL_ERROR."""
    events: list[str] = []
    graph = FakeGraph(events)
    graph.ainvoke = _raising_invoke(TimeoutError("cancel scope timed out"))
    audit = FakeAudit(events)
    service = make_service(events, make_settings(tmp_path), audit=audit, graph=graph)

    with pytest.raises(RequestTimedOut):
        await service.ask(make_request())

    assert len(audit.failed_errors) == 1
    assert audit.failed_errors[0]["code"] == "REQUEST_TIMED_OUT"
    assert events.index("audit:fail_request") < events.index("lease:release")


async def test_model_endpoint_failure_maps_to_502_upstream_failed(tmp_path):
    """OpenAI-compatible endpoint failures (MaaS outages) must surface as
    UPSTREAM_FAILED (502), not INTERNAL_ERROR (500)."""
    events: list[str] = []
    graph = FakeGraph(events)
    graph.ainvoke = _raising_invoke(
        APIConnectionError(
            request=httpx.Request("POST", "https://maas.example/v1/chat/completions")
        )
    )
    audit = FakeAudit(events)
    service = make_service(events, make_settings(tmp_path), audit=audit, graph=graph)

    with pytest.raises(UpstreamFailed):
        await service.ask(make_request())

    assert len(audit.failed_errors) == 1
    assert audit.failed_errors[0]["code"] == "UPSTREAM_FAILED"


async def test_state_db_failure_inside_graph_maps_to_persistence_failed(
    tmp_path,
):
    """Raw psycopg errors from the lease renew loop or the LangGraph
    checkpointer must surface as PERSISTENCE_FAILED (500, state store),
    not INTERNAL_ERROR."""
    events: list[str] = []
    graph = FakeGraph(events)
    graph.ainvoke = _raising_invoke(OperationalError("connection reset by peer"))
    audit = FakeAudit(events)
    service = make_service(events, make_settings(tmp_path), audit=audit, graph=graph)

    with pytest.raises(PersistenceFailed):
        await service.ask(make_request())

    assert len(audit.failed_errors) == 1
    assert audit.failed_errors[0]["code"] == "PERSISTENCE_FAILED"


async def test_state_db_failure_on_lease_acquire_maps_to_persistence_failed(
    tmp_path,
):
    """Lease acquisition happens before the audit request is created; a state
    DB failure there must still map to PERSISTENCE_FAILED, not 500 generic."""

    class FailingLeases:
        async def acquire(self, session_id, *, ttl):
            raise OperationalError("pool is closed")

        async def renew(self, lease, *, ttl):
            return True

        async def release(self, lease):
            return True

    events: list[str] = []
    audit = FakeAudit(events)
    service = make_service(
        events,
        make_settings(tmp_path),
        leases=FailingLeases(),
        audit=audit,
    )

    with pytest.raises(PersistenceFailed):
        await service.ask(make_request())

    assert audit.start_calls == 0


def test_public_error_messages_stay_actionable_without_admin_referral():
    """Public messages must not contain filler like "若持续出现请联系管理员";
    every message should tell the user what to do, per NN/g guidance."""
    concrete_errors = [
        error_class
        for _, error_class in inspect.getmembers(errors_module, inspect.isclass)
        if issubclass(error_class, ChatServiceError)
        and error_class is not ChatServiceError
    ]

    assert len(concrete_errors) >= 12
    for error_class in concrete_errors:
        assert "管理员" not in error_class.public_message
        assert "若持续出现" not in error_class.public_message
        assert error_class.public_message.strip().endswith(("。", "？"))
