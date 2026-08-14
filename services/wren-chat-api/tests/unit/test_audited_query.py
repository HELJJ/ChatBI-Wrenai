"""Unit contracts for the pre-execution audited Wren query component."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pyarrow as pa
import pytest

from wren_chat_api.audit import (
    AttemptAlreadyTerminal,
    AuditRepositoryError,
    SqlAttemptLimitReached,
    StartedAttempt,
)
from wren_chat_api.audited_query import AuditedQuery, RunContext
from wren_chat_api.config import Settings
from wren_chat_api.errors import PersistenceFailed


def make_settings(tmp_path) -> Settings:
    return Settings(
        state_database_url="postgresql://user:pass@localhost:5432/wren_test",
        api_key="integration-test-key",
        project_path=tmp_path,
        model="test-model",
    )


class FakeAudit:
    def __init__(self, events, *, failures=None) -> None:
        self.events = events
        self.failures = failures or {}
        self.executed_sql_calls: list[str] = []
        self.failed_attempts: list[dict] = []

    async def start_attempt(self, *, request_id, semantic_sql, row_limit):
        self.events.append("audit:start_attempt")
        failure = self.failures.get("start_attempt")
        if failure is not None:
            raise failure
        return StartedAttempt(attempt_id=uuid4(), sequence=1)

    async def set_executed_sql(self, *, attempt_id, executed_sql):
        self.events.append("audit:set_executed_sql")
        failure = self.failures.get("set_executed_sql")
        if failure is not None:
            raise failure
        self.executed_sql_calls.append(executed_sql)

    async def succeed_attempt(
        self, *, attempt_id, result, returned_row_count, result_truncated
    ):
        self.events.append("audit:succeed_attempt")
        failure = self.failures.get("succeed_attempt")
        if failure is not None:
            raise failure

    async def fail_attempt(self, *, attempt_id, error):
        self.events.append("audit:fail_attempt")
        failure = self.failures.get("fail_attempt")
        if failure is not None:
            raise failure
        self.failed_attempts.append(error.model_dump())


class FakeToolkit:
    def __init__(
        self,
        events,
        *,
        rows=(1,),
        fail_plan: bool = False,
        fail_execute: bool = False,
    ) -> None:
        self.events = events
        self.rows = list(rows)
        self.fail_plan = fail_plan
        self.fail_execute = fail_execute
        self.plan_calls = 0
        self.execute_calls = 0

    def plan_query(self, sql):
        self.events.append("wren:plan")
        self.plan_calls += 1
        if self.fail_plan:
            raise RuntimeError("plan boom")
        return SimpleNamespace(dialect_sql=f"PLANNED::{sql}")

    def execute_planned(self, plan, limit):
        self.events.append(f"wren:execute_planned:{limit}")
        self.execute_calls += 1
        if self.fail_execute:
            raise RuntimeError("execute boom")
        return pa.table({"value": self.rows})


class InlineExecutor:
    async def run(self, func, *args):
        return func(*args)


class CancellingExecutor:
    async def run(self, func, *args):
        raise asyncio.CancelledError()


def build_component(events, *, settings, toolkit=None, audit=None):
    toolkit = toolkit if toolkit is not None else FakeToolkit(events)
    audit = audit if audit is not None else FakeAudit(events)
    component = AuditedQuery(
        audit=audit,
        toolkit=toolkit,
        settings=settings,
        executor=InlineExecutor(),
    )
    return component, audit, toolkit


def make_context() -> RunContext:
    return RunContext(request_id=uuid4(), session_id="session-1")


async def test_running_attempt_and_executed_sql_are_persisted_before_query(
    tmp_path,
) -> None:
    events: list[str] = []
    component, audit, _ = build_component(
        events, settings=make_settings(tmp_path)
    )

    result = await component.execute(make_context(), "SELECT 1", limit=100)

    assert result["ok"] is True
    assert result["returned_row_count"] == 1
    assert result["result_truncated"] is False
    assert result["content_truncated"] is False
    assert isinstance(result["content"], str)
    assert "rows" not in result and "result" not in result
    assert audit.executed_sql_calls == ["PLANNED::SELECT 1"]
    assert events == [
        "audit:start_attempt",
        "wren:plan",
        "audit:set_executed_sql",
        "wren:execute_planned:101",
        "audit:succeed_attempt",
    ]


async def test_planning_failure_records_no_executed_sql(tmp_path) -> None:
    events: list[str] = []
    toolkit = FakeToolkit(events, fail_plan=True)
    component, audit, _ = build_component(
        events, settings=make_settings(tmp_path), toolkit=toolkit
    )

    result = await component.execute(make_context(), "SELECT 1")

    assert result["ok"] is False
    assert result["error"]["code"] == "SQL_ATTEMPT_FAILED"
    assert result["error"]["phase"] == "SQL_PLANNING"
    assert audit.executed_sql_calls == []
    assert events == [
        "audit:start_attempt",
        "wren:plan",
        "audit:fail_attempt",
    ]


async def test_execution_failure_retains_executed_sql(tmp_path) -> None:
    events: list[str] = []
    toolkit = FakeToolkit(events, fail_execute=True)
    component, audit, _ = build_component(
        events, settings=make_settings(tmp_path), toolkit=toolkit
    )

    result = await component.execute(make_context(), "SELECT 1")

    assert result["ok"] is False
    assert result["error"]["phase"] == "SQL_EXECUTION"
    assert audit.executed_sql_calls == ["PLANNED::SELECT 1"]
    assert events == [
        "audit:start_attempt",
        "wren:plan",
        "audit:set_executed_sql",
        "wren:execute_planned:101",
        "audit:fail_attempt",
    ]


async def test_read_only_rejection_is_reported_without_planning(tmp_path) -> None:
    events: list[str] = []
    component, _, toolkit = build_component(
        events, settings=make_settings(tmp_path)
    )

    result = await component.execute(make_context(), "DELETE FROM orders")

    assert result["ok"] is False
    assert result["error"]["code"] == "READ_ONLY_SQL_REQUIRED"
    assert result["error"]["phase"] == "SQL_PLANNING"
    assert toolkit.plan_calls == 0
    assert toolkit.execute_calls == 0
    assert events == ["audit:start_attempt", "audit:fail_attempt"]


async def test_start_attempt_failure_skips_wren_entirely(tmp_path) -> None:
    events: list[str] = []
    audit = FakeAudit(
        events, failures={"start_attempt": AuditRepositoryError("db down")}
    )
    component, _, toolkit = build_component(
        events, settings=make_settings(tmp_path), audit=audit
    )

    with pytest.raises(PersistenceFailed):
        await component.execute(make_context(), "SELECT 1")

    assert toolkit.plan_calls == 0
    assert toolkit.execute_calls == 0
    assert events == ["audit:start_attempt"]


async def test_attempt_limit_is_returned_without_touching_wren(tmp_path) -> None:
    events: list[str] = []
    audit = FakeAudit(
        events, failures={"start_attempt": SqlAttemptLimitReached("limit")}
    )
    component, _, toolkit = build_component(
        events, settings=make_settings(tmp_path), audit=audit
    )

    result = await component.execute(make_context(), "SELECT 1")

    assert result["ok"] is False
    assert result["error"]["code"] == "SQL_RETRY_EXHAUSTED"
    assert toolkit.plan_calls == 0
    assert toolkit.execute_calls == 0
    assert events == ["audit:start_attempt"]


@pytest.mark.parametrize(
    "action,fail_execute",
    [
        ("set_executed_sql", False),
        ("succeed_attempt", False),
        ("fail_attempt", True),
    ],
)
async def test_audit_persistence_failure_stops_the_request(
    tmp_path, action, fail_execute
) -> None:
    events: list[str] = []
    toolkit = FakeToolkit(events, fail_execute=fail_execute)
    audit = FakeAudit(
        events, failures={action: AttemptAlreadyTerminal("already terminal")}
    )
    component, _, _ = build_component(
        events, settings=make_settings(tmp_path), toolkit=toolkit, audit=audit
    )

    with pytest.raises(PersistenceFailed):
        await component.execute(make_context(), "SELECT 1")

    if action == "fail_attempt":
        assert "audit:fail_attempt" in events
    else:
        assert "audit:fail_attempt" not in events


async def test_cancellation_is_reraised_without_terminalizing(tmp_path) -> None:
    events: list[str] = []
    component = AuditedQuery(
        audit=FakeAudit(events),
        toolkit=FakeToolkit(events),
        settings=make_settings(tmp_path),
        executor=CancellingExecutor(),
    )

    with pytest.raises(asyncio.CancelledError):
        await component.execute(make_context(), "SELECT 1")

    assert events == ["audit:start_attempt"]
