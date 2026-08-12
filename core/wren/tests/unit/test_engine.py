"""Unit tests for WrenEngine — no database required.

transpile() and dry_plan() exercise the wren-core MDL planning + sqlglot
transpile path without connecting to any data source.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import orjson
import pyarrow as pa
import pytest

from wren import WrenEngine
from wren.config import WrenConfig
from wren.engine import PlannedQuery
from wren.model.data_source import DataSource
from wren.model.error import DIALECT_SQL, ErrorCode, ErrorPhase, WrenError

pytestmark = pytest.mark.unit

# Minimal manifest with a single model.  No real DB needed for planning.
_MANIFEST = {
    "catalog": "wren",
    "schema": "public",
    "models": [
        {
            "name": "orders",
            "tableReference": {"schema": "main", "table": "orders"},
            "columns": [
                {"name": "o_orderkey", "type": "integer"},
                {"name": "o_custkey", "type": "integer"},
                {"name": "o_orderstatus", "type": "varchar"},
                {
                    "name": "order_cust_key",
                    "type": "varchar",
                    "expression": "concat(cast(o_orderkey as varchar), '_', cast(o_custkey as varchar))",
                },
            ],
            "primaryKey": "o_orderkey",
        }
    ],
}
_MANIFEST_STR = base64.b64encode(orjson.dumps(_MANIFEST)).decode()


@pytest.fixture(scope="module")
def duckdb_engine(tmp_path_factory):
    """A WrenEngine pointed at a temporary DuckDB file (not queried by unit tests)."""
    db_dir = tmp_path_factory.mktemp("unit_duckdb")
    conn_info = {"url": str(db_dir), "format": "duckdb"}
    with WrenEngine(_MANIFEST_STR, DataSource.duckdb, conn_info, fallback=False) as e:
        yield e


@pytest.fixture(scope="module")
def pg_engine():
    """A WrenEngine configured for Postgres (no real connection opened for planning)."""
    conn_info = {
        "host": "localhost",
        "port": 5432,
        "database": "test",
        "user": "test",
        "password": "test",
    }
    with WrenEngine(_MANIFEST_STR, DataSource.postgres, conn_info, fallback=False) as e:
        yield e


# ------------------------------------------------------------------
# dry_plan (no DB access)
# ------------------------------------------------------------------


def test_dry_plan_returns_string(duckdb_engine: WrenEngine) -> None:
    sql = duckdb_engine.dry_plan('SELECT o_orderkey FROM "orders" LIMIT 1')
    assert isinstance(sql, str)
    assert len(sql) > 0


def test_dry_plan_postgres_dialect(pg_engine: WrenEngine) -> None:
    """dry_plan should produce Postgres-flavoured SQL (no backtick quoting, etc.)."""
    sql = pg_engine.dry_plan('SELECT o_orderkey FROM "orders" LIMIT 1')
    assert isinstance(sql, str)
    # sqlglot Postgres output uses double-quote identifiers, not backticks
    assert "`" not in sql


def test_dry_plan_calculated_field(duckdb_engine: WrenEngine) -> None:
    sql = duckdb_engine.dry_plan('SELECT order_cust_key FROM "orders" LIMIT 1')
    assert isinstance(sql, str)
    # The calculated column expression should be expanded in the SQL
    assert "concat" in sql.lower() or "||" in sql.lower()


def test_dry_plan_invalid_sql_raises(duckdb_engine: WrenEngine) -> None:
    with pytest.raises(WrenError):
        duckdb_engine.dry_plan("SELECT * FROM not_a_model_in_manifest")


# ------------------------------------------------------------------
# Two-phase query planning and execution
# ------------------------------------------------------------------


def test_plan_query_returns_planned_target_sql(duckdb_engine: WrenEngine) -> None:
    plan = duckdb_engine.plan_query('SELECT o_orderkey FROM "orders" LIMIT 1')

    assert isinstance(plan, PlannedQuery)
    assert isinstance(plan.dialect_sql, str)
    assert plan.dialect_sql


def test_query_plans_once_then_executes_exact_plan(
    duckdb_engine: WrenEngine, monkeypatch
) -> None:
    plan = PlannedQuery(dialect_sql="SELECT 1 AS value")
    table = pa.table({"value": [1]})
    connector = MagicMock()
    connector.query.return_value = table
    plan_query = MagicMock(return_value=plan)
    monkeypatch.setattr(duckdb_engine, "plan_query", plan_query)
    monkeypatch.setattr(duckdb_engine, "_get_connector", lambda: connector)

    result = duckdb_engine.query("SELECT 1", limit=7)

    assert result is table
    plan_query.assert_called_once_with("SELECT 1", None)
    connector.query.assert_called_once_with("SELECT 1 AS value", 7)


def test_execute_planned_does_not_plan_again(
    duckdb_engine: WrenEngine, monkeypatch
) -> None:
    table = pa.table({"value": [1]})
    connector = MagicMock()
    connector.query.return_value = table
    monkeypatch.setattr(duckdb_engine, "_get_connector", lambda: connector)
    monkeypatch.setattr(
        duckdb_engine,
        "_plan",
        MagicMock(side_effect=AssertionError("must not re-plan")),
    )

    result = duckdb_engine.execute_planned(
        PlannedQuery(dialect_sql="SELECT 1 AS value"), limit=2
    )

    assert result is table
    connector.query.assert_called_once_with("SELECT 1 AS value", 2)


def test_execute_planned_wraps_connector_failure(
    duckdb_engine: WrenEngine, monkeypatch
) -> None:
    connector = MagicMock()
    connector.query.side_effect = RuntimeError("database unavailable")
    monkeypatch.setattr(duckdb_engine, "_get_connector", lambda: connector)
    plan = PlannedQuery(dialect_sql="SELECT * FROM physical_orders")

    with pytest.raises(WrenError) as exc_info:
        duckdb_engine.execute_planned(plan)

    error = exc_info.value
    assert error.error_code == ErrorCode.GENERIC_USER_ERROR
    assert error.phase == ErrorPhase.SQL_EXECUTION
    assert error.metadata == {DIALECT_SQL: plan.dialect_sql}


def test_execute_planned_preserves_structured_wren_error(
    duckdb_engine: WrenEngine, monkeypatch
) -> None:
    expected = WrenError(
        ErrorCode.DATABASE_TIMEOUT,
        "query timed out",
        phase=ErrorPhase.SQL_EXECUTION,
        metadata={"driver": "postgres"},
    )
    connector = MagicMock()
    connector.query.side_effect = expected
    monkeypatch.setattr(duckdb_engine, "_get_connector", lambda: connector)

    with pytest.raises(WrenError) as exc_info:
        duckdb_engine.execute_planned(PlannedQuery(dialect_sql="SELECT 1"))

    assert exc_info.value is expected


# ------------------------------------------------------------------
# Context manager
# ------------------------------------------------------------------


def test_context_manager_closes_connector() -> None:
    conn_info = {"url": "/tmp", "format": "duckdb"}
    with WrenEngine(_MANIFEST_STR, DataSource.duckdb, conn_info, fallback=False) as e:
        assert e._connector is None  # connector is lazily initialized

    # After __exit__, internal state is cleaned up
    assert e._connector is None


# ------------------------------------------------------------------
# Strict mode (no DB access)
# ------------------------------------------------------------------

_STRICT_CONFIG = WrenConfig(strict_mode=True)
_BLACKLIST_CONFIG = WrenConfig(denied_functions=frozenset(["pg_read_file"]))


def test_strict_mode_blocks_unknown_table():
    conn_info = {"url": "/tmp", "format": "duckdb"}
    with WrenEngine(
        _MANIFEST_STR,
        DataSource.duckdb,
        conn_info,
        fallback=False,
        config=_STRICT_CONFIG,
    ) as engine:
        with pytest.raises(WrenError) as exc_info:
            engine.dry_plan("SELECT * FROM secret_table")
        assert exc_info.value.error_code == ErrorCode.MODEL_NOT_FOUND


def test_strict_mode_allows_mdl_table():
    conn_info = {"url": "/tmp", "format": "duckdb"}
    with WrenEngine(
        _MANIFEST_STR,
        DataSource.duckdb,
        conn_info,
        fallback=False,
        config=_STRICT_CONFIG,
    ) as engine:
        sql = engine.dry_plan('SELECT o_orderkey FROM "orders" LIMIT 1')
        assert isinstance(sql, str)
        assert len(sql) > 0


def test_strict_mode_blocks_denied_function():
    conn_info = {"url": "/tmp", "format": "duckdb"}
    with WrenEngine(
        _MANIFEST_STR,
        DataSource.duckdb,
        conn_info,
        fallback=False,
        config=_BLACKLIST_CONFIG,
    ) as engine:
        with pytest.raises(WrenError) as exc_info:
            engine.dry_plan("SELECT pg_read_file('/etc/passwd')")
        assert exc_info.value.error_code == ErrorCode.BLOCKED_FUNCTION


def test_non_strict_mode_allows_unknown_table(duckdb_engine: WrenEngine):
    # Default config (no strict mode) — non-MDL tables should not be blocked
    # by policy (may still fail during planning, but not with MODEL_NOT_FOUND)
    try:
        duckdb_engine.dry_plan("SELECT * FROM unknown_table")
    except WrenError as e:
        assert e.error_code != ErrorCode.MODEL_NOT_FOUND
