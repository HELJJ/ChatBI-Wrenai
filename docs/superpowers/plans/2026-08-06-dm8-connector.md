# DM8 Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only DM8 support to the Wren Python SDK, CLI, profiles, and MCP path using the official `dmPython` driver.

**Architecture:** `dm8` is a first-class Python `DataSource` with a native DB-API connector. SQL parsing/rendering and the Rust session boundary explicitly alias DM8 to Oracle, because the observed server accepts Oracle constructs but rejects MySQL backtick identifiers. The Rust crates remain unchanged.

**Tech Stack:** Python 3.11+, Pydantic 2, dmPython 2.5+, PyArrow, SQLGlot, pytest, uv/ruff.

## Global Constraints

- Only read/query support is in scope; do not add write or schema-mutation APIs.
- Use `dmPython>=2.5` as an optional `dm8` extra and include it in `all`.
- Never log, commit, or embed real credentials in source or tests.
- Validate schema as a simple identifier before interpolating it into `SET SCHEMA`.
- Map DM8 to Oracle at both SQLGlot and wren-core boundaries; do not add a Rust `DataSource::DM8`.
- The supplied workspace has no `.git` metadata, so commit steps are recorded as checkpoints but cannot be executed here.

---

## File Structure

- Create `core/wren/src/wren/connector/dm8.py`: dmPython connection, schema selection, query/dry-run, Arrow conversion, and error translation.
- Create `core/wren/tests/unit/test_dm8_connection.py`: connection/model/schema behavior with a fake dmPython module.
- Create `core/wren/tests/unit/test_dm8_connector.py`: query, limit, dry-run, Arrow, errors, and close behavior.
- Modify `core/wren/src/wren/model/__init__.py`: define/export `DM8ConnectionInfo` and add it to `ConnectionInfo`.
- Modify `core/wren/src/wren/model/data_source.py`: expose `DataSource.dm8` and build its connection model.
- Modify `core/wren/src/wren/model/field_registry.py`: register DM8 fields for CLI/UI/docs.
- Modify `core/wren/src/wren/connector/factory.py`: route DM8 to the native connector.
- Modify `core/wren/src/wren/mdl/cte_rewriter.py`: map DM8 to SQLGlot Oracle.
- Modify `core/wren/src/wren/engine.py` and `core/wren/src/wren/mcp_server.py`: map DM8 to the Rust-supported Oracle session name through one helper.
- Modify `core/wren/pyproject.toml`: package the optional driver.
- Modify `core/wren/README.md` and `core/wren/src/wren/context.py`: document DM8 installation and availability.
- Modify existing unit tests for registry, factory, and dialect/session mapping.

---

### Task 1: DM8 Data Source and Connection Model

**Files:**
- Create: `core/wren/tests/unit/test_dm8_connection.py`
- Modify: `core/wren/src/wren/model/__init__.py`
- Modify: `core/wren/src/wren/model/data_source.py`
- Modify: `core/wren/src/wren/model/field_registry.py`
- Modify: `core/wren/tests/test_field_registry.py`

**Interfaces:**
- Produces: `DM8ConnectionInfo(host: str, port: StrPort = "5236", user: str, password: SecretStr | None, schema: str | None)`.
- Produces: `DataSource.dm8` and `DataSource.dm8.get_connection_info(data)`.
- Produces: `DATASOURCE_MODELS["dm8"] == [DM8ConnectionInfo]`.

- [ ] **Step 1: Write failing model and registry tests**

```python
def test_dm8_connection_info_defaults_and_masks_password():
    info = DataSource.dm8.get_connection_info(
        {"host": "db", "user": "app", "password": "secret"}
    )
    assert isinstance(info, DM8ConnectionInfo)
    assert info.port == "5236"
    assert info.schema is None
    assert info.password.get_secret_value() == "secret"


def test_dm8_fields_are_selectable():
    assert "dm8" in get_selectable_datasources()
    fields = {field.name: field for field in get_fields("dm8")}
    assert set(fields) == {"host", "port", "user", "password", "schema"}
    assert fields["password"].sensitive is True
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `cd core/wren && uv run --no-sync pytest tests/unit/test_dm8_connection.py tests/test_field_registry.py -q`

Expected: collection or assertion failure because `DM8ConnectionInfo` and `DataSource.dm8` do not exist.

- [ ] **Step 3: Add the connection model and registrations**

Implement exactly these fields:

```python
class DM8ConnectionInfo(BaseConnectionInfo):
    host: str = Field(examples=["localhost"])
    port: StrPort = Field(default="5236", examples=[5236])
    user: str = Field(examples=["SYSDBA"])
    password: SecretStr | None = Field(default=None)
    schema: str | None = Field(default=None, examples=["APP"])
```

Add the class to `ConnectionInfo`, `DataSource._build_connection_info`, imports,
and `DATASOURCE_MODELS`. Keep field order stable for interactive prompts.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `cd core/wren && uv run --no-sync pytest tests/unit/test_dm8_connection.py tests/test_field_registry.py -q`

Expected: PASS.

- [ ] **Step 5: Checkpoint**

Review the diff for model/registry-only changes. In a Git checkout, commit as
`feat: add dm8 connection model`.

---

### Task 2: Planning Dialect Aliases

**Files:**
- Modify: `core/wren/src/wren/mdl/cte_rewriter.py`
- Modify: `core/wren/src/wren/mdl/__init__.py`
- Modify: `core/wren/src/wren/engine.py`
- Modify: `core/wren/src/wren/mcp_server.py`
- Modify: `core/wren/tests/unit/test_cte_rewriter.py`
- Create: `core/wren/tests/unit/test_dm8_dialect.py`

**Interfaces:**
- Produces: `get_sqlglot_dialect(DataSource.dm8) -> "oracle"`.
- Produces: `get_core_data_source(DataSource | str | None) -> str | None`, returning `"oracle"` for DM8 and otherwise preserving the current name/value.
- Produces: `normalize_manifest_for_core(str | None) -> str | None`, aliasing a top-level manifest `dataSource: dm8` before Rust deserialization.
- Consumes: `DataSource.dm8` from Task 1.

- [ ] **Step 1: Write failing alias tests**

```python
def test_dm8_uses_oracle_sqlglot_dialect():
    assert get_sqlglot_dialect(DataSource.dm8) == "oracle"


def test_dm8_uses_oracle_core_data_source():
    assert get_core_data_source(DataSource.dm8) == "oracle"
    assert get_core_data_source("dm8") == "oracle"
    assert get_core_data_source(DataSource.postgres) == "postgres"
    assert get_core_data_source(None) is None
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd core/wren && uv run --no-sync pytest tests/unit/test_dm8_dialect.py tests/unit/test_cte_rewriter.py -q`

Expected: FAIL because DM8 alias helpers are absent.

- [ ] **Step 3: Implement explicit aliases**

Add `DataSource.dm8: "oracle"` to `_SQLGLOT_DIALECT_MAP`. Add this helper near
`get_session_context`:

```python
def get_core_data_source(data_source: DataSource | str | None) -> str | None:
    if data_source is None:
        return None
    value = data_source.value if isinstance(data_source, DataSource) else data_source
    return "oracle" if value.lower() == "dm8" else value.lower()
```

Use the helpers at the centralized Python wrappers for wren-core
`SessionContext` and `ManifestExtractor`, which covers the engine and MCP
dry-plan paths. Do not change the manifest dialect allow-list.

- [ ] **Step 4: Run alias and existing CTE tests**

Run: `cd core/wren && uv run --no-sync pytest tests/unit/test_dm8_dialect.py tests/unit/test_cte_rewriter.py -q`

Expected: PASS with Oracle quoting for DM8.

- [ ] **Step 5: Checkpoint**

Review that no literal `dm8` reaches `wren_core.SessionContext`. In a Git
checkout, commit as `feat: map dm8 planning to oracle`.

---

### Task 3: Native dmPython Connector

**Files:**
- Create: `core/wren/src/wren/connector/dm8.py`
- Create: `core/wren/tests/unit/test_dm8_connector.py`
- Modify: `core/wren/src/wren/connector/factory.py`
- Modify: `core/wren/tests/unit/test_connector_factory.py`

**Interfaces:**
- Produces: `DM8Connector(connection_info: DM8ConnectionInfo)` implementing `ConnectorABC`.
- Produces: `create_connector(connection_info) -> DM8Connector`.
- Consumes: `DM8ConnectionInfo` and `DataSource.dm8` from Task 1.

- [ ] **Step 1: Write fake-driver behavior tests**

Use fake connection/cursor objects injected into `sys.modules["dmPython"]` before
importing the connector. Tests must assert observable calls:

```python
def test_connects_and_selects_valid_schema(fake_dm):
    info = DM8ConnectionInfo(
        host="db", port="5236", user="app", password="secret", schema="APP"
    )
    connector = create_connector(info)
    assert fake_dm.connect_kwargs == {
        "server": "db", "port": 5236, "user": "app", "password": "secret"
    }
    assert fake_dm.connection.executed[0] == 'SET SCHEMA "APP"'
    connector.close()


def test_query_wraps_limit_and_returns_arrow_table(fake_dm):
    connector = create_connector(dm8_info())
    table = connector.query("SELECT amount FROM orders;", limit=2)
    assert fake_dm.connection.executed[-1] == (
        "SELECT * FROM (SELECT amount FROM orders) t WHERE ROWNUM <= 2"
    )
    assert table.column_names == ["AMOUNT"]


def test_dry_run_uses_zero_row_wrapper(fake_dm):
    connector = create_connector(dm8_info())
    connector.dry_run("SELECT * FROM orders;")
    assert fake_dm.connection.executed[-1] == (
        "SELECT * FROM (SELECT * FROM orders) t WHERE ROWNUM <= 0"
    )
```

Also test invalid schemas (`APP;DROP`, `A.B`, empty after validation), driver
`DatabaseError` translation, decimal/date/bytes/null Arrow values, and calling
`close()` twice.

- [ ] **Step 2: Run connector tests and verify failure**

Run: `cd core/wren && uv run --no-sync pytest tests/unit/test_dm8_connector.py tests/unit/test_connector_factory.py -q`

Expected: FAIL because the connector and factory entry do not exist.

- [ ] **Step 3: Implement minimal connector**

Use `coerce_limit` and `strip_trailing_semicolon`. Validate schema with
`re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#]*", schema)`. Construct Arrow columns
from `cursor.description` names and fetched Python values using `pa.array`,
normalizing driver objects exposing `read()` before inference. Wrap driver
errors as:

```python
raise WrenError(
    ErrorCode.INVALID_SQL,
    str(exc),
    phase=ErrorPhase.SQL_EXECUTION,
    metadata={DIALECT_SQL: executed_sql},
) from exc
```

Use `SQL_DRY_RUN` for dry-run failures. Register
`DataSource.dm8: "wren.connector.dm8"` in the factory.

- [ ] **Step 4: Run connector and shared base tests**

Run: `cd core/wren && uv run --no-sync pytest tests/unit/test_dm8_connector.py tests/unit/test_connector_factory.py -q`

Expected: PASS.

- [ ] **Step 5: Checkpoint**

Review error paths for secret leakage and cursor cleanup. In a Git checkout,
commit as `feat: add native dmPython connector`.

---

### Task 4: Packaging and User-Facing Documentation

**Files:**
- Modify: `core/wren/pyproject.toml`
- Modify: `core/wren/README.md`
- Modify: `core/wren/src/wren/context.py`
- Modify: `README.md`
- Create: `core/wren/tests/unit/test_dm8_packaging.py`

**Interfaces:**
- Produces: install commands `pip install 'wrenai[dm8]'` and inclusion in `wrenai[all]`.
- Documents: `data_source: dm8` and environment-backed password profiles.

- [ ] **Step 1: Add packaging assertion**

Create `tests/unit/test_dm8_packaging.py` with a focused TOML parser test:

```python
from pathlib import Path
import tomllib


def test_dm8_optional_dependency_is_packaged():
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text())["project"]
    extras = project["optional-dependencies"]
    assert extras["dm8"] == ["dmPython>=2.5"]
    assert "dm8" in extras["all"][0]
```

- [ ] **Step 2: Run the packaging test and verify failure**

Run: `cd core/wren && uv run --no-sync pytest tests/unit/test_dm8_packaging.py -q`

Expected: FAIL because `dm8` is absent.

- [ ] **Step 3: Add the optional dependency and concise docs**

Add:

```toml
dm8 = ["dmPython>=2.5"]
```

Include `dm8` in the `all` extra and datasource lists. Document this safe
profile pattern without a real password:

```yaml
datasource: dm8
host: dm.example.internal
port: "5236"
user: APP
password: ${DM_PASSWORD}
schema: APP
```

- [ ] **Step 4: Run packaging/docs guards**

Run: `cd core/wren && uv run --no-sync pytest tests/unit/test_served_content_guard.py tests/unit/test_version.py -q`

Expected: PASS.

- [ ] **Step 5: Checkpoint**

Review that no real host, username, or password is present. In a Git checkout,
commit as `docs: document dm8 connector setup`.

---

### Task 5: Integrated Verification

**Files:**
- Modify only files required by failures demonstrably caused by Tasks 1-4.

**Interfaces:**
- Consumes all prior task outputs.
- Produces a tested DM8 installation/profile/query path.

- [ ] **Step 1: Run the complete unit suite**

Run: `cd core/wren && uv run --no-sync pytest tests/unit/ -q -m "unit and not slow"`

Expected: PASS.

- [ ] **Step 2: Run field/profile integration tests**

Run: `cd core/wren && uv run --no-sync pytest tests/test_field_registry.py tests/test_profile.py tests/test_profile_cli.py -q`

Expected: PASS. If a named file is absent, use `rg --files tests | rg 'profile'`
and run the existing profile test files returned by the repository.

- [ ] **Step 3: Run lint**

Run: `cd core/wren && uv run --no-sync ruff format --check src/ tests/unit/test_dm8_*.py && uv run --no-sync ruff check src/ tests/unit/test_dm8_*.py`

Expected: PASS with no formatting or lint errors.

- [ ] **Step 4: Verify package metadata**

Run: `cd core/wren && uv build`

Expected: wheel and sdist build successfully and advertise the `dm8` extra.

- [ ] **Step 5: Provide the real-instance smoke test**

On the user's Linux host with `DM_PASSWORD` exported, run:

```bash
pip install -e '/mnt/sdb/workspace/WrenAI-main/core/wren[dm8]'
wren docs connection-info dm8
wren profile add dm8-prod --interactive
wren context build
wren dry-plan --sql 'SELECT * FROM "YourModel"'
wren query --sql 'SELECT * FROM "YourModel"' --limit 1
```

Expected: profile validation succeeds, dry-plan emits Oracle-compatible quoting,
and the limited query returns one row from DM8.

- [ ] **Step 6: Final checkpoint**

Record exact test/build outputs and any test not runnable in the current
environment. In a Git checkout, commit the final fixes as
`test: cover dm8 connector integration`.
