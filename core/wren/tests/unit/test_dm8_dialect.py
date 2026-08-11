from __future__ import annotations

import base64
import json

import pytest

import wren.mdl as mdl
from wren.mdl.cte_rewriter import get_sqlglot_dialect
from wren.model.data_source import DataSource

pytestmark = pytest.mark.unit


def _manifest_with_data_source(data_source: str) -> str:
    manifest = {
        "catalog": "wren",
        "schema": "public",
        "models": [],
        "relationships": [],
        "views": [],
        "dataSource": data_source,
    }
    return base64.b64encode(json.dumps(manifest).encode()).decode()


def test_dm8_uses_oracle_sqlglot_dialect() -> None:
    assert get_sqlglot_dialect(DataSource.dm8) == "oracle"


def test_dm8_uses_oracle_at_the_core_session_boundary(monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_session_context(*args):
        calls.append(args)
        return object()

    monkeypatch.setattr(mdl.wren_core, "SessionContext", fake_session_context)
    mdl.get_session_context.cache_clear()

    mdl.get_session_context("manifest", None, None, "dm8")

    assert calls == [("manifest", None, None, "oracle")]
    mdl.get_session_context.cache_clear()


def test_core_data_source_alias_preserves_supported_sources() -> None:
    helper = getattr(mdl, "get_core_data_source", None)

    assert helper is not None
    assert helper(DataSource.dm8) == "oracle"
    assert helper("dm8") == "oracle"
    assert helper(DataSource.postgres) == "postgres"
    assert helper("POSTGRES") == "postgres"
    assert helper(None) is None


def test_dm8_manifest_is_aliased_before_manifest_extraction(monkeypatch) -> None:
    captured: list[str] = []

    def fake_manifest_extractor(manifest_str: str):
        captured.append(manifest_str)
        return object()

    monkeypatch.setattr(mdl.wren_core, "ManifestExtractor", fake_manifest_extractor)

    mdl.get_manifest_extractor(_manifest_with_data_source("dm8"))

    decoded = json.loads(base64.b64decode(captured[0]))
    assert decoded["dataSource"] == "oracle"
