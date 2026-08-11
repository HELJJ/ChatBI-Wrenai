from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from wren.connector.factory import get_connector
from wren.model.data_source import DataSource
from wren.model.error import DIALECT_SQL, ErrorCode, ErrorPhase, WrenError

pytestmark = pytest.mark.unit


class FakeDatabaseError(Exception):
    pass


class FakeCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.description = None
        self.closed = False

    def execute(self, sql: str) -> None:
        self.connection.executed.append(sql)
        if self.connection.raise_on_execute:
            raise FakeDatabaseError("DM execution failed")
        self.description = self.connection.description

    def fetchall(self):
        return self.connection.rows

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.description = [
            ("AMOUNT", None, None, None, None, None, None),
            ("CREATED_ON", None, None, None, None, None, None),
            ("PAYLOAD", None, None, None, None, None, None),
            ("OPTIONAL_VALUE", None, None, None, None, None, None),
        ]
        self.rows = [(Decimal("12.50"), date(2026, 8, 6), b"dm8", None)]
        self.raise_on_execute = False
        self.close_calls = 0
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.close_calls += 1


class FakeDMModule(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(DatabaseError=FakeDatabaseError)
        self.connection = FakeConnection()
        self.connect_calls: list[dict] = []

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        return self.connection


@pytest.fixture
def fake_dm(monkeypatch):
    module = FakeDMModule()
    monkeypatch.setitem(sys.modules, "dmPython", module)
    sys.modules.pop("wren.connector.dm8", None)
    yield module
    sys.modules.pop("wren.connector.dm8", None)


def dm8_info(**overrides):
    data = {
        "host": "dm.internal",
        "port": "5236",
        "user": "app",
        "password": "secret",
    }
    data.update(overrides)
    return DataSource.dm8.get_connection_info(data)


def test_connects_with_dm_python_and_selects_schema(fake_dm) -> None:
    connector = get_connector(DataSource.dm8, dm8_info(schema="APP"))

    assert fake_dm.connect_calls == [
        {
            "server": "dm.internal",
            "port": 5236,
            "user": "app",
            "password": "secret",
        }
    ]
    assert fake_dm.connection.executed == ['SET SCHEMA "APP"']
    assert fake_dm.connection.cursors[0].closed is True
    connector.close()


def test_rejects_schema_that_could_inject_sql_before_connecting(fake_dm) -> None:
    with pytest.raises(WrenError) as exc:
        get_connector(DataSource.dm8, dm8_info(schema="APP; DROP TABLE users"))

    assert exc.value.error_code == ErrorCode.INVALID_CONNECTION_INFO
    assert fake_dm.connect_calls == []


def test_query_wraps_limit_and_returns_arrow_values(fake_dm) -> None:
    connector = get_connector(DataSource.dm8, dm8_info())

    table = connector.query("SELECT amount FROM orders;", limit=2)

    assert fake_dm.connection.executed == [
        "SELECT * FROM (SELECT amount FROM orders) t WHERE ROWNUM <= 2"
    ]
    assert table.column_names == [
        "AMOUNT",
        "CREATED_ON",
        "PAYLOAD",
        "OPTIONAL_VALUE",
    ]
    assert table.to_pylist() == [
        {
            "AMOUNT": Decimal("12.50"),
            "CREATED_ON": date(2026, 8, 6),
            "PAYLOAD": b"dm8",
            "OPTIONAL_VALUE": None,
        }
    ]
    assert fake_dm.connection.cursors[-1].closed is True


def test_dry_run_uses_zero_row_wrapper(fake_dm) -> None:
    connector = get_connector(DataSource.dm8, dm8_info())

    connector.dry_run("SELECT * FROM orders;  ")

    assert fake_dm.connection.executed == [
        "SELECT * FROM (SELECT * FROM orders) t WHERE ROWNUM <= 0"
    ]
    assert fake_dm.connection.cursors[-1].closed is True


def test_driver_error_includes_executed_sql_metadata(fake_dm) -> None:
    connector = get_connector(DataSource.dm8, dm8_info())
    fake_dm.connection.raise_on_execute = True

    with pytest.raises(WrenError) as exc:
        connector.query("SELECT broken FROM orders")

    assert exc.value.error_code == ErrorCode.INVALID_SQL
    assert exc.value.phase == ErrorPhase.SQL_EXECUTION
    assert exc.value.metadata == {DIALECT_SQL: "SELECT broken FROM orders"}
    assert fake_dm.connection.cursors[-1].closed is True


def test_close_is_idempotent(fake_dm) -> None:
    connector = get_connector(DataSource.dm8, dm8_info())

    connector.close()
    connector.close()

    assert fake_dm.connection.close_calls == 1
