"""MDL processing utilities backed by wren-core-py."""

import base64
import json
from functools import cache

import wren_core

from wren.model.data_source import DataSource


def get_core_data_source(data_source: DataSource | str | None) -> str | None:
    """Map Python-only data sources to a dialect supported by wren-core."""
    if data_source is None:
        return None
    value = data_source.value if isinstance(data_source, DataSource) else data_source
    normalized = value.lower()
    return "oracle" if normalized == "dm8" else normalized


def normalize_manifest_for_core(manifest_str: str | None) -> str | None:
    """Alias Python-only manifest data sources before Rust deserialization."""
    if manifest_str is None:
        return None
    try:
        manifest = json.loads(base64.b64decode(manifest_str))
    except (TypeError, ValueError):
        # Preserve the original input so wren-core remains the validation
        # boundary and produces its established error for malformed manifests.
        return manifest_str
    if not isinstance(manifest, dict):
        return manifest_str
    data_source = manifest.get("dataSource")
    if not isinstance(data_source, str) or data_source.lower() != "dm8":
        return manifest_str

    manifest["dataSource"] = "oracle"
    payload = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(payload.encode()).decode()


@cache
def get_session_context(
    manifest_str: str | None,
    function_path: str | None,
    properties: frozenset | None = None,
    data_source: str | None = None,
) -> wren_core.SessionContext:
    return wren_core.SessionContext(
        normalize_manifest_for_core(manifest_str),
        function_path,
        properties,
        get_core_data_source(data_source),
    )


def get_manifest_extractor(manifest_str: str) -> wren_core.ManifestExtractor:
    return wren_core.ManifestExtractor(normalize_manifest_for_core(manifest_str))


def to_json_base64(manifest) -> str:
    return wren_core.to_json_base64(manifest)


def transform_sql(
    manifest_str: str,
    sql: str,
    data_source: str | None = None,
    function_path: str | None = None,
    properties: dict | None = None,
) -> str:
    """Transform SQL through wren-core MDL processing.

    Returns the planned SQL string (dialect-neutral DataFusion SQL).
    """
    processed = None
    if properties:
        processed = frozenset(properties.items())

    session = get_session_context(manifest_str, function_path, processed, data_source)
    return session.transform_sql(sql)
