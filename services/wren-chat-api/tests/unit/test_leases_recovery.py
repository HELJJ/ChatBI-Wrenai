"""Unit contracts for lease validation and the recovery scheduler."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from wren_chat_api.leases import LeaseRepository
from wren_chat_api.recovery import (
    RecoveryCounts,
    recover_interrupted,
    run_recovery_loop,
)


class FakeCursor:
    def __init__(self, *, row=None, rows=None, rowcount=0) -> None:
        self.row = row
        self.rows = rows or []
        self.rowcount = rowcount

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return self.rows


class AsyncContext:
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(self, cursors) -> None:
        self.cursors = iter(cursors)
        self.calls = []
        self.transaction_entries = 0

    async def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters))
        return next(self.cursors)

    def transaction(self):
        connection = self

        class Transaction(AsyncContext):
            async def __aenter__(self):
                connection.transaction_entries += 1
                return await super().__aenter__()

        return Transaction(None)


class PostgreSQLTypeCheckingConnection(FakeConnection):
    """Reject untyped parameters passed to polymorphic JSON functions."""

    async def execute(self, statement, parameters=None):
        if "'message', %s," in statement:
            raise RuntimeError("PostgreSQL cannot infer the message parameter type")
        return await super().execute(statement, parameters)


class FakePool:
    def __init__(self, connection) -> None:
        self.connection_value = connection

    def connection(self):
        return AsyncContext(self.connection_value)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
async def test_acquire_rejects_non_positive_ttl(ttl: timedelta) -> None:
    leases = LeaseRepository(None)

    with pytest.raises(ValueError, match="ttl must be positive"):
        await leases.acquire("session-1", ttl=ttl)


async def test_lease_lifecycle_passes_ownership_token_to_updates() -> None:
    lease_id = uuid4()
    expires_at = datetime(2026, 8, 13, 12, 1, tzinfo=timezone.utc)
    connection = FakeConnection(
        [
            FakeCursor(
                row={
                    "session_id": "session-1",
                    "lease_id": lease_id,
                    "expires_at": expires_at,
                }
            ),
            FakeCursor(rowcount=1),
            FakeCursor(rowcount=1),
        ]
    )
    leases = LeaseRepository(FakePool(connection))

    lease = await leases.acquire("session-1", ttl=timedelta(seconds=30))
    assert lease is not None
    assert await leases.renew(lease, ttl=timedelta(seconds=45)) is True
    assert await leases.release(lease) is True

    acquire_parameters = connection.calls[0][1]
    renew_parameters = connection.calls[1][1]
    release_parameters = connection.calls[2][1]
    assert acquire_parameters[0] == "session-1"
    assert acquire_parameters[2] == timedelta(seconds=30)
    assert renew_parameters == (
        timedelta(seconds=45),
        "session-1",
        lease_id,
    )
    assert release_parameters == ("session-1", lease_id)


async def test_recovery_rejects_naive_timestamp_before_using_pool() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await recover_interrupted(
            None,
            now=datetime(2026, 8, 13, 12, 0),
            threshold=timedelta(seconds=150),
        )


async def test_recovery_rejects_non_positive_threshold() -> None:
    with pytest.raises(ValueError, match="threshold must be positive"):
        await recover_interrupted(
            None,
            now=datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            threshold=timedelta(0),
        )


async def test_recovery_updates_attempts_before_requests_in_one_transaction() -> None:
    recovery_time = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    connection = FakeConnection(
        [
            FakeCursor(rows=[{"request_id": uuid4()}, {"request_id": uuid4()}]),
            FakeCursor(rows=[{"request_id": uuid4()}]),
        ]
    )

    counts = await recover_interrupted(
        FakePool(connection),
        now=recovery_time,
        threshold=timedelta(seconds=150),
    )

    assert counts == RecoveryCounts(
        attempts_recovered=2,
        requests_recovered=1,
    )
    assert connection.transaction_entries == 1
    assert len(connection.calls) == 2
    assert "UPDATE chat_sql_attempts" in connection.calls[0][0]
    assert "UPDATE chat_audit_requests" in connection.calls[1][0]
    assert connection.calls[0][1][1] == recovery_time
    assert connection.calls[1][1][1] == recovery_time


async def test_recovery_types_json_message_parameters_for_postgresql() -> None:
    connection = PostgreSQLTypeCheckingConnection(
        [
            FakeCursor(rows=[]),
            FakeCursor(rows=[]),
        ]
    )

    counts = await recover_interrupted(
        FakePool(connection),
        now=datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
        threshold=timedelta(seconds=150),
    )

    assert counts == RecoveryCounts(
        attempts_recovered=0,
        requests_recovered=0,
    )


async def test_recovery_loop_runs_immediately_and_stops(monkeypatch) -> None:
    stop_event = asyncio.Event()
    recover = AsyncMock(
        return_value=RecoveryCounts(
            attempts_recovered=0,
            requests_recovered=0,
        )
    )

    async def recover_once(*args, **kwargs):
        result = await recover(*args, **kwargs)
        stop_event.set()
        return result

    monkeypatch.setattr(
        "wren_chat_api.recovery.recover_interrupted",
        recover_once,
    )

    await run_recovery_loop(
        stop_event=stop_event,
        pool=object(),
        interval_seconds=30,
        threshold_seconds=150,
    )

    recover.assert_awaited_once()
