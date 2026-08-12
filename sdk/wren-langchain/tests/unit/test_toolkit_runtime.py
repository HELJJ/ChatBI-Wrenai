"""Tests for WrenToolkit runtime API: query, dry_plan, dry_run."""

import base64
import json
from unittest.mock import MagicMock, patch

import pyarrow as pa
from wren.config import WrenConfig

from wren_langchain import PlannedQuery, WrenToolkit


def test_query_invokes_wren_engine_with_resolved_manifest(
    tmp_project, fake_active_profile
):
    """toolkit.query reads the manifest fresh and delegates to WrenEngine.query."""
    fake_table = pa.table({"x": [1, 2, 3]})
    fake_engine = MagicMock(name="WrenEngine")
    fake_engine.query.return_value = fake_table
    fake_engine._connector = MagicMock(name="connector")

    toolkit = WrenToolkit.from_project(tmp_project)

    with patch(
        "wren_langchain._toolkit.WrenEngine", return_value=fake_engine
    ) as engine_ctor:
        result = toolkit.query("SELECT 1", limit=10)

    assert result is fake_table
    fake_engine.query.assert_called_once_with("SELECT 1", limit=10)
    # Engine constructed with manifest bytes + datasource + connection_info
    engine_ctor.assert_called_once()
    kwargs = engine_ctor.call_args.kwargs
    assert kwargs["data_source"] == "duckdb"
    assert kwargs["connection_info"] == {"path": ":memory:"}


def test_connector_is_reused_across_query_calls(tmp_project, fake_active_profile):
    """Second query reuses the cached connector instead of reconnecting.

    Distinguishes "what engine 2 starts with" from "what gets injected" by
    seeding each fresh engine with a different connector. If reuse is broken,
    engine 2's `_connector` would remain its own initial mock; if reuse works,
    it gets replaced with engine 1's connector before `query()` runs.
    """
    first_connector = MagicMock(name="first_connector")
    second_initial_connector = MagicMock(name="second_initial_connector")
    engines = []

    def make_engine(*args, **kwargs):
        engine = MagicMock(name=f"engine{len(engines)}")
        engine._connector = first_connector if not engines else second_initial_connector
        engines.append(engine)
        return engine

    toolkit = WrenToolkit.from_project(tmp_project)

    with patch("wren_langchain._toolkit.WrenEngine", side_effect=make_engine):
        toolkit.query("SELECT 1")
        toolkit.query("SELECT 2")

    # Engine 1 keeps its connector; engine 2's initial connector got
    # overwritten with the cached one from engine 1 before query() ran.
    assert engines[0]._connector is first_connector
    assert engines[1]._connector is first_connector
    assert engines[1]._connector is not second_initial_connector


def test_manifest_is_read_through_on_every_call(
    tmp_project, fake_active_profile, monkeypatch
):
    """Each query re-reads target/mdl.json so external CLI rebuilds are picked up."""
    fake_engine = MagicMock(name="engine")
    fake_engine._connector = MagicMock()

    toolkit = WrenToolkit.from_project(tmp_project)

    # Replace the manifest content between calls.
    mdl_path = tmp_project / "target" / "mdl.json"
    mdl_path.write_text('{"models": [{"name": "v1"}]}')

    with patch(
        "wren_langchain._toolkit.WrenEngine", return_value=fake_engine
    ) as engine_ctor:
        toolkit.query("SELECT 1")

        # Simulate `wren context build` updating the file.
        mdl_path.write_text('{"models": [{"name": "v2"}]}')
        toolkit.query("SELECT 2")

    first_manifest_b64 = engine_ctor.call_args_list[0].kwargs["manifest_str"]
    second_manifest_b64 = engine_ctor.call_args_list[1].kwargs["manifest_str"]
    first = json.loads(base64.b64decode(first_manifest_b64))
    second = json.loads(base64.b64decode(second_manifest_b64))
    assert first["models"][0]["name"] == "v1"
    assert second["models"][0]["name"] == "v2"


def test_dry_plan_delegates_to_engine(tmp_project, fake_active_profile):
    fake_engine = MagicMock(name="engine")
    fake_engine.dry_plan.return_value = "SELECT * FROM cte_orders"
    fake_engine._connector = MagicMock()

    toolkit = WrenToolkit.from_project(tmp_project)

    with patch("wren_langchain._toolkit.WrenEngine", return_value=fake_engine):
        result = toolkit.dry_plan("SELECT * FROM orders")

    assert result == "SELECT * FROM cte_orders"
    fake_engine.dry_plan.assert_called_once_with("SELECT * FROM orders")


def test_dry_run_delegates_to_engine(tmp_project, fake_active_profile):
    fake_engine = MagicMock(name="engine")
    fake_engine._connector = MagicMock()

    toolkit = WrenToolkit.from_project(tmp_project)

    with patch("wren_langchain._toolkit.WrenEngine", return_value=fake_engine):
        toolkit.dry_run("SELECT 1")

    fake_engine.dry_run.assert_called_once_with("SELECT 1")


def test_plan_then_execute_reuses_exact_plan(tmp_project, fake_active_profile):
    plan = PlannedQuery(dialect_sql="SELECT 1 AS value")
    table = pa.table({"value": [1]})
    fake_engine = MagicMock(name="engine")
    fake_engine.plan_query.return_value = plan
    fake_engine.execute_planned.return_value = table
    fake_engine._connector = MagicMock(name="connector")
    toolkit = WrenToolkit.from_project(tmp_project)

    with patch("wren_langchain._toolkit.WrenEngine", return_value=fake_engine):
        actual_plan = toolkit.plan_query("SELECT 1")
        actual_table = toolkit.execute_planned(actual_plan, limit=101)

    assert actual_plan is plan
    assert actual_table is table
    fake_engine.plan_query.assert_called_once_with("SELECT 1")
    fake_engine.execute_planned.assert_called_once_with(plan, limit=101)


def test_execute_planned_reuses_connector_across_calls(
    tmp_project, fake_active_profile
):
    first_connector = MagicMock(name="first_connector")
    second_initial_connector = MagicMock(name="second_initial_connector")
    engines = []

    def make_engine(*args, **kwargs):
        engine = MagicMock(name=f"engine{len(engines)}")
        engine._connector = first_connector if not engines else second_initial_connector
        engines.append(engine)
        return engine

    toolkit = WrenToolkit.from_project(tmp_project)

    with patch("wren_langchain._toolkit.WrenEngine", side_effect=make_engine):
        toolkit.execute_planned(PlannedQuery("SELECT 1"))
        toolkit.execute_planned(PlannedQuery("SELECT 2"))

    assert engines[1]._connector is first_connector


def test_toolkit_forwards_strict_wren_config(tmp_project, fake_active_profile):
    config = WrenConfig(strict_mode=True)
    toolkit = WrenToolkit.from_project(tmp_project, config=config)
    fake_engine = MagicMock(name="engine")

    with patch(
        "wren_langchain._toolkit.WrenEngine", return_value=fake_engine
    ) as engine_ctor:
        toolkit.dry_plan("SELECT * FROM orders")

    assert engine_ctor.call_args.kwargs["config"] is config


def test_toolkit_default_wren_config_remains_non_strict(
    tmp_project, fake_active_profile
):
    toolkit = WrenToolkit.from_project(tmp_project)
    fake_engine = MagicMock(name="engine")

    with patch(
        "wren_langchain._toolkit.WrenEngine", return_value=fake_engine
    ) as engine_ctor:
        toolkit.dry_plan("SELECT 1")

    config = engine_ctor.call_args.kwargs["config"]
    assert config == WrenConfig()
    assert config.strict_mode is False
