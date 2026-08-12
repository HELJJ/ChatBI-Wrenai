# Session-Isolated Wren Chat API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a separately deployable JSON API that maintains conversation context by `session_id`, lets an LLM answer business-data questions through Wren, and durably audits every SQL attempt before it is planned or executed.

**Architecture:** Extend `WrenEngine` and `WrenToolkit` with a two-phase planned-query API, then build `services/wren-chat-api` around FastAPI, a custom sequential LangGraph ReAct graph, and PostgreSQL. Application audit/lease transactions use one async psycopg pool; LangGraph checkpoints use a separate autocommit pool so checkpoint connection semantics cannot leak into business audit transactions.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, LangChain 1.x, LangGraph 1.2+, `langgraph-checkpoint-postgres` 3.1+, psycopg 3, PostgreSQL 15+, PyArrow, sqlglot, pytest, testcontainers, Ruff, Uvicorn, Prometheus client.

## Global Constraints

- The public request contains only `session_id` and `question`; unknown fields are rejected.
- A successful response contains exactly `session_id` and `answer`.
- Invalid request JSON or fields return the stable error envelope with HTTP `400`; HTTP `422` is reserved for a well-formed question that cannot produce a valid data answer.
- There is no tenant or user identity dimension. The caller must not reuse one `session_id` for unrelated conversations.
- Internal `thread_id` is `"wren-chat:" + sha256(session_id.encode("utf-8")).hexdigest()`.
- Only one active request may hold a lease for a given `session_id`; different sessions may run concurrently.
- Insert every SQL attempt with `status=running` before Wren planning. If that insert fails, do not plan or execute SQL.
- Persist `executed_sql` while the attempt is still running and before giving it to the Wren connector.
- SQL attempt states are `running -> success|failed`; terminal updates must use `WHERE status='running'`.
- Recover interrupted attempts only when the session lease has expired and the attempt is older than 150 seconds. Use `SQL_ATTEMPT_INTERRUPTED` with `metadata.outcome="unknown"`.
- Default result limit is 100 rows; hard maximum is 1,000; fetch `N+1` rows to determine `result_truncated`.
- Result limits apply to returned rows, not to database-side aggregation.
- Persist every normalized row up to `row_limit` in the audit result (subject only to the 1 MiB serialized-result ceiling). Separately cap LLM-facing tool content at 64 KiB and mark that tool content as truncated; never redefine audit `result_truncated` to mean prompt-content truncation.
- Maximum SQL attempts per request is 3; maximum graph steps is 12; total request timeout is 120 seconds.
- Preserve the latest six complete question-answer turns and summarize older turns. Do not keep completed SQL retry messages or result rows in the latest durable conversation state.
- Permit one read-only query statement only. Reject DML, DDL, transaction commands, and multiple statements before connector execution. Run Wren with strict MDL mode and a read-only business database credential.
- Do not expose `wren_store_query` to the service agent.
- Do not add an audit-management HTTP API in this plan. Internal users inspect audit tables with PostgreSQL tools and application logs with the deployment runtime.
- Use `langchain-core>=1.2.22`, `langgraph>=1.2.8`, `langgraph-checkpoint>=3.0.0`, and `langgraph-checkpoint-postgres>=3.1.0` to include published security fixes.
- Never enable pickle fallback or deserialize caller-controlled LangChain objects.
- Follow repository Ruff configuration, Conventional Commits, and the repository Contribution Bar.

---

## File Structure

### Existing modules modified

- `core/wren/src/wren/engine.py` — define `PlannedQuery`, split planning from connector execution, and preserve `query()` compatibility.
- `core/wren/tests/unit/test_engine.py` — prove planning occurs once, the connector receives the planned SQL, and errors retain phase metadata.
- `sdk/wren-langchain/src/wren_langchain/_toolkit.py` — expose two-phase planning/execution, explicitly forward immutable `WrenConfig`, and retain connector reuse and manifest read-through.
- `sdk/wren-langchain/src/wren_langchain/__init__.py` — export the SDK-facing planned-query type if public exports are explicit.
- `sdk/wren-langchain/tests/unit/test_toolkit_runtime.py` — verify the new toolkit contract.
- `.github/workflows/wren-chat-api-ci.yml` — lint, unit, PostgreSQL integration, and build checks for the new service.

### New service package

- `services/wren-chat-api/pyproject.toml` — package metadata, runtime dependencies, dev dependencies, and pytest/Ruff configuration.
- `services/wren-chat-api/src/wren_chat_api/config.py` — validated environment configuration.
- `services/wren-chat-api/src/wren_chat_api/contracts.py` — public API and internal audit Pydantic models.
- `services/wren-chat-api/src/wren_chat_api/errors.py` — stable internal exceptions and public error mapping.
- `services/wren-chat-api/src/wren_chat_api/identity.py` — deterministic thread ID derivation.
- `services/wren-chat-api/src/wren_chat_api/db.py` — application and checkpointer pool lifecycle plus migration runner.
- `services/wren-chat-api/migrations/0001_chat_audit.sql` — audit request, SQL attempt, lease, and migration tables with constraints/indexes.
- `services/wren-chat-api/src/wren_chat_api/audit.py` — request/attempt repository and canonical audit reads.
- `services/wren-chat-api/src/wren_chat_api/leases.py` — atomic lease acquisition, renewal, and release.
- `services/wren-chat-api/src/wren_chat_api/recovery.py` — idempotent interrupted-attempt/request recovery loop.
- `services/wren-chat-api/src/wren_chat_api/sql_policy.py` — one-statement, read-only SQL validation.
- `services/wren-chat-api/src/wren_chat_api/results.py` — PyArrow result probing, truncation, JSON normalization, size enforcement, and redaction.
- `services/wren-chat-api/src/wren_chat_api/executor.py` — bounded submission and lifecycle for blocking Wren calls.
- `services/wren-chat-api/src/wren_chat_api/audited_query.py` — pre-inserted, two-phase, audited Wren query execution.
- `services/wren-chat-api/src/wren_chat_api/agent.py` — sequential ReAct graph, request runtime context, retry budget, and conversation compaction.
- `services/wren-chat-api/src/wren_chat_api/chat.py` — lease/audit/graph orchestration and terminal state transitions.
- `services/wren-chat-api/src/wren_chat_api/auth.py` — constant-time bearer-key verification.
- `services/wren-chat-api/src/wren_chat_api/metrics.py` — bounded Prometheus metrics without raw session labels.
- `services/wren-chat-api/src/wren_chat_api/app.py` — FastAPI factory, lifespan, routes, exception handlers, and health checks.
- `services/wren-chat-api/src/wren_chat_api/main.py` — Uvicorn import target.
- `services/wren-chat-api/tests/` — unit, PostgreSQL integration, graph, API, and DuckDB end-to-end tests.
- `services/wren-chat-api/Dockerfile` — repository-root build-context image.
- `services/wren-chat-api/compose.yml` — local PostgreSQL and API development stack.
- `services/wren-chat-api/.env.example` — documented non-secret configuration names.
- `services/wren-chat-api/README.md` — setup, API use, audit queries, operations, and constraints.

---

### Task 1: Add a Two-Phase Planned Query API to Wren Core and WrenToolkit

**Files:**
- Modify: `core/wren/src/wren/engine.py`
- Modify: `core/wren/tests/unit/test_engine.py`
- Modify: `sdk/wren-langchain/src/wren_langchain/_toolkit.py`
- Modify: `sdk/wren-langchain/src/wren_langchain/__init__.py`
- Modify: `sdk/wren-langchain/tests/unit/test_toolkit_runtime.py`

**Interfaces:**
- Produces: `wren.engine.PlannedQuery(dialect_sql: str)`.
- Produces: `WrenEngine.plan_query(sql: str, properties: dict | None = None) -> PlannedQuery`.
- Produces: `WrenEngine.execute_planned(plan: PlannedQuery, limit: int | None = None) -> pyarrow.Table`.
- Preserves: `WrenEngine.dry_plan(...) -> str` and `WrenEngine.query(...) -> pyarrow.Table`.
- Produces: `WrenToolkit.plan_query(sql: str) -> PlannedQuery` and `WrenToolkit.execute_planned(plan: PlannedQuery, limit: int | None = None) -> pyarrow.Table`.
- Produces: `WrenToolkit.from_project(..., config: WrenConfig | None = None)` and forwards the same immutable config to every read-through `WrenEngine` instance.

- [ ] **Step 1: Write failing WrenEngine contract tests**

Add direct behavior tests that invoke methods rather than inspect source:

```python
from unittest.mock import MagicMock

import pyarrow as pa

from wren.engine import PlannedQuery


def test_query_plans_once_then_executes_planned_sql(duckdb_engine, monkeypatch):
    table = pa.table({"value": [1]})
    connector = MagicMock()
    connector.query.return_value = table
    monkeypatch.setattr(duckdb_engine, "_get_connector", lambda: connector)
    expected_sql = duckdb_engine.plan_query("SELECT 1").dialect_sql
    plan_spy = MagicMock(wraps=duckdb_engine.plan_query)
    monkeypatch.setattr(duckdb_engine, "plan_query", plan_spy)

    result = duckdb_engine.query("SELECT 1", limit=7)

    assert result is table
    assert plan_spy.call_count == 1
    connector.query.assert_called_once_with(expected_sql, 7)


def test_execute_planned_does_not_plan_again(duckdb_engine, monkeypatch):
    connector = MagicMock()
    connector.query.return_value = pa.table({"value": [1]})
    monkeypatch.setattr(duckdb_engine, "_get_connector", lambda: connector)
    monkeypatch.setattr(
        duckdb_engine,
        "dry_plan",
        MagicMock(side_effect=AssertionError("must not re-plan")),
    )

    plan = PlannedQuery(dialect_sql="SELECT 1 AS value")
    duckdb_engine.execute_planned(plan, limit=2)

    connector.query.assert_called_once_with("SELECT 1 AS value", 2)
```

- [ ] **Step 2: Run the focused core tests and verify the API is absent**

Run:

```powershell
Set-Location core/wren
uv run --no-sync pytest tests/unit/test_engine.py -v
```

Expected: FAIL because `PlannedQuery`, `plan_query`, and `execute_planned` do not exist.

- [ ] **Step 3: Implement `PlannedQuery` and delegate the old API**

Use an immutable, JSON-free value object and keep connector error wrapping in one place:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    dialect_sql: str


def plan_query(
    self, sql: str, properties: dict | None = None
) -> PlannedQuery:
    return PlannedQuery(dialect_sql=self._plan(sql, properties))


def dry_plan(self, sql: str, properties: dict | None = None) -> str:
    return self.plan_query(sql, properties).dialect_sql


def execute_planned(
    self, plan: PlannedQuery, limit: int | None = None
) -> pa.Table:
    connector = self._get_connector()
    try:
        return connector.query(plan.dialect_sql, limit)
    except WrenError:
        raise
    except Exception as exc:
        raise WrenError(
            ErrorCode.GENERIC_USER_ERROR,
            str(exc),
            phase=ErrorPhase.SQL_EXECUTION,
            metadata={DIALECT_SQL: plan.dialect_sql},
        ) from exc


def query(self, sql, limit=None, properties=None) -> pa.Table:
    return self.execute_planned(self.plan_query(sql, properties), limit)
```

- [ ] **Step 4: Write failing toolkit two-phase tests**

```python
def test_toolkit_plan_then_execute_reuses_exact_plan(
    tmp_project, fake_active_profile
):
    plan = PlannedQuery("SELECT 1 AS value")
    table = pa.table({"value": [1]})
    fake_engine = MagicMock()
    fake_engine.plan_query.return_value = plan
    fake_engine.execute_planned.return_value = table
    fake_engine._connector = MagicMock()
    toolkit = WrenToolkit.from_project(tmp_project)

    with patch("wren_langchain._toolkit.WrenEngine", return_value=fake_engine):
        actual_plan = toolkit.plan_query("SELECT 1")
        actual_table = toolkit.execute_planned(actual_plan, limit=101)

    assert actual_plan is plan
    assert actual_table is table
    fake_engine.plan_query.assert_called_once_with("SELECT 1")
    fake_engine.execute_planned.assert_called_once_with(plan, limit=101)
```

Add a second test that constructs the toolkit with an explicit immutable config and proves every engine receives it:

```python
def test_toolkit_forwards_strict_wren_config(tmp_project):
    config = WrenConfig(strict_mode=True)
    toolkit = WrenToolkit.from_project(tmp_project, config=config)

    with patch("wren_langchain._toolkit.WrenEngine") as engine_cls:
        toolkit.dry_plan("SELECT * FROM orders")

    assert engine_cls.call_args.kwargs["config"] is config
```

- [ ] **Step 5: Implement toolkit delegation, strict config forwarding, and public export**

Add `config: WrenConfig | None = None` to both the toolkit constructor and `from_project()`, store `self._config = config or WrenConfig()`, and pass `config=self._config` in `_build_engine()`. Add the two query-phase methods beside `query()` and apply the same connector cache transfer in `execute_planned()` that `query()` currently uses. Export `PlannedQuery` from `wren_langchain.__init__` so the service does not import a private module. The existing default remains non-strict for backward compatibility; the new service must opt into strict mode explicitly.

- [ ] **Step 6: Run core and LangChain SDK tests**

Run:

```powershell
Set-Location core/wren
uv run --no-sync pytest tests/unit/test_engine.py -v
Set-Location ../../sdk/wren-langchain
pytest tests/unit/test_toolkit_runtime.py tests/unit/test_tools_runtime.py -v
```

Expected: PASS, including existing `query()` behavior.

- [ ] **Step 7: Commit**

```powershell
git add core/wren/src/wren/engine.py core/wren/tests/unit/test_engine.py sdk/wren-langchain/src/wren_langchain/_toolkit.py sdk/wren-langchain/src/wren_langchain/__init__.py sdk/wren-langchain/tests/unit/test_toolkit_runtime.py
git commit -m "feat: expose planned Wren query execution"
```

---

### Task 2: Scaffold the Service, Configuration, Contracts, and Error Taxonomy

**Files:**
- Create: `services/wren-chat-api/pyproject.toml`
- Create: `services/wren-chat-api/src/wren_chat_api/__init__.py`
- Create: `services/wren-chat-api/src/wren_chat_api/config.py`
- Create: `services/wren-chat-api/src/wren_chat_api/contracts.py`
- Create: `services/wren-chat-api/src/wren_chat_api/errors.py`
- Create: `services/wren-chat-api/src/wren_chat_api/identity.py`
- Create: `services/wren-chat-api/tests/unit/test_config.py`
- Create: `services/wren-chat-api/tests/unit/test_contracts.py`

**Interfaces:**
- Produces: `Settings` with all bounded defaults from Global Constraints.
- Produces: `ChatRequest`, `ChatResponse`, `ErrorBody`, `ErrorResponse`, `AttemptError`, `AttemptResult`.
- Produces: `derive_thread_id(session_id: str) -> str`.
- Produces: typed `ChatServiceError` subclasses with `code`, `http_status`, and public message.

- [ ] **Step 1: Create package metadata with security floors**

Declare these runtime dependencies exactly:

```toml
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "pydantic>=2.10",
  "pydantic-settings>=2.7",
  "langchain>=1.2",
  "langchain-core>=1.2.22",
  "langchain-openai>=1.1",
  "langgraph>=1.2.8",
  "langgraph-checkpoint>=3.0.0",
  "langgraph-checkpoint-postgres>=3.1.0",
  "psycopg[binary,pool]>=3.2.7",
  "prometheus-client>=0.21",
  "pyarrow>=14",
  "sqlglot>=29",
  "wrenai>=0.13.2",
  "wren-langchain>=0.2.1",
]
```

Dev dependencies: `pytest>=8`, `pytest-asyncio>=0.25`, `httpx>=0.28`, `testcontainers[postgres]>=4`, `ruff>=0.9`, and `build>=1.2`. Because this service consumes APIs added to the monorepo packages in Task 1, local development, CI, and Docker must install `core/wren` and `sdk/wren-langchain` from this checkout before installing the service; they must not accidentally test against older PyPI wheels.

- [ ] **Step 2: Write failing validation and identity tests**

```python
import pytest
from pydantic import ValidationError

from wren_chat_api.contracts import ChatRequest, ChatResponse
from wren_chat_api.identity import derive_thread_id


def test_chat_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ChatRequest(session_id="s-1", question="count orders", extra="x")


@pytest.mark.parametrize("session_id", ["", "has space", "x" * 129])
def test_chat_request_rejects_invalid_session_id(session_id):
    with pytest.raises(ValidationError):
        ChatRequest(session_id=session_id, question="count orders")


def test_success_contract_has_exactly_two_fields():
    response = ChatResponse(session_id="s-1", answer="42")
    assert response.model_dump() == {"session_id": "s-1", "answer": "42"}


def test_thread_id_is_stable_and_does_not_contain_raw_session():
    first = derive_thread_id("private-session")
    assert first == derive_thread_id("private-session")
    assert first.startswith("wren-chat:")
    assert "private-session" not in first
```

- [ ] **Step 3: Run tests and verify imports fail**

Run: `pytest services/wren-chat-api/tests/unit/test_config.py services/wren-chat-api/tests/unit/test_contracts.py -v`

Expected: FAIL because the service modules do not exist.

- [ ] **Step 4: Implement strict Pydantic contracts and settings**

Use `ConfigDict(extra="forbid")`, `StringConstraints`, and `SecretStr`. Required environment values are:

```text
WREN_CHAT_DATABASE_URL
WREN_CHAT_API_KEY
WREN_CHAT_PROJECT_PATH
WREN_CHAT_MODEL
```

Bounded defaults:

```python
question_max_chars: int = 4_000
default_row_limit: int = 100
max_row_limit: int = 1_000
max_result_bytes: int = 1_048_576
max_tool_content_bytes: int = 65_536
max_sql_attempts: int = 3
graph_recursion_limit: int = 12
request_timeout_seconds: int = 120
lease_ttl_seconds: int = 30
lease_renew_seconds: int = 10
interruption_threshold_seconds: int = 150
recent_turns: int = 6
wren_workers: int = 16
wren_queue_capacity: int = 32
recovery_interval_seconds: int = 30
```

Implement `derive_thread_id()` exactly as specified in Global Constraints.

- [ ] **Step 5: Implement stable service exceptions**

Create subclasses for `AuthenticationFailed`, `SessionBusy`, `QuestionUnanswerable`, `CapacityExceeded`, `PersistenceFailed`, `UpstreamFailed`, and `RequestTimedOut`. Each contains a non-sensitive `public_message`; internal exceptions are stored separately through `AttemptError` or request audit error JSON.

- [ ] **Step 6: Run unit tests and Ruff**

Run:

```powershell
Set-Location services/wren-chat-api
pytest tests/unit/test_config.py tests/unit/test_contracts.py -v
ruff check src tests
ruff format --check src tests
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add services/wren-chat-api
git commit -m "feat: scaffold Wren chat API contracts"
```

---

### Task 3: Create PostgreSQL Migrations and the Audit Repository

**Files:**
- Create: `services/wren-chat-api/migrations/0001_chat_audit.sql`
- Create: `services/wren-chat-api/src/wren_chat_api/db.py`
- Create: `services/wren-chat-api/src/wren_chat_api/audit.py`
- Create: `services/wren-chat-api/tests/integration/conftest.py`
- Create: `services/wren-chat-api/tests/integration/test_migrations.py`
- Create: `services/wren-chat-api/tests/integration/test_audit_repository.py`

**Interfaces:**
- Produces: `create_app_pool(settings) -> AsyncConnectionPool` using normal transactional connections.
- Produces: `create_checkpoint_pool(settings) -> AsyncConnectionPool` using `autocommit=True`, `prepare_threshold=0`, and `dict_row`.
- Produces: `apply_migrations(pool, migrations_dir) -> None`.
- Produces: `AuditRepository.start_request`, `succeed_request`, `fail_request`, `start_attempt`, `set_executed_sql`, `succeed_attempt`, `fail_attempt`, and `get_canonical_audit`.

- [ ] **Step 1: Write the migration SQL with database-enforced states**

Create the schema explicitly (do not rely on ORM auto-creation):

- `chat_audit_requests`: `request_id UUID PRIMARY KEY`, original `session_id TEXT NOT NULL`, derived `thread_id TEXT NOT NULL`, original `question TEXT NOT NULL`, nullable `answer TEXT`, `status TEXT NOT NULL`, nullable `error JSONB`, required `started_at TIMESTAMPTZ`, nullable `completed_at TIMESTAMPTZ`;
- `chat_sql_attempts`: the columns listed in the Interfaces above, with `request_id` referencing `chat_audit_requests(request_id)` and no cascading delete of audit evidence;
- `chat_session_leases`: `session_id TEXT PRIMARY KEY`, `lease_id UUID NOT NULL`, `expires_at TIMESTAMPTZ NOT NULL`, and `updated_at TIMESTAMPTZ NOT NULL`;
- `wren_chat_schema_migrations`: `version TEXT PRIMARY KEY` and `applied_at TIMESTAMPTZ NOT NULL`.

Enforce request status values `running|succeeded|failed` and these terminal invariants: a running request has no answer/error/completion, a succeeded request has an answer and completion but no error, and a failed request has error and completion but no answer. The attempt constraint must be equivalent to:

```sql
CHECK (
  (status = 'running'
    AND result IS NULL AND error IS NULL
    AND completed_at IS NULL AND duration_ms IS NULL)
  OR
  (status = 'success'
    AND result IS NOT NULL AND error IS NULL
    AND completed_at IS NOT NULL AND duration_ms IS NOT NULL)
  OR
  (status = 'failed'
    AND result IS NULL AND error IS NOT NULL
    AND completed_at IS NOT NULL AND duration_ms IS NOT NULL)
)
```

Also enforce attempt status values `running|success|failed`, `UNIQUE(request_id, sequence)`, `sequence >= 1`, positive row limits, non-negative counts/durations, and `returned_row_count <= row_limit`. Add indexes on request `(session_id, started_at DESC)`, `thread_id`, `(status, started_at)`, and attempt `(request_id, sequence)`. Migration application must use the migrations table and a PostgreSQL advisory lock so two service replicas cannot apply the same migration concurrently.

- [ ] **Step 2: Write failing PostgreSQL integration tests**

Tests must execute the migration and then prove invalid states are rejected:

```python
async def test_running_attempt_must_not_have_result(audit_repo, request_id):
    with pytest.raises(psycopg.errors.CheckViolation):
        async with audit_repo.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO chat_sql_attempts
                (attempt_id, request_id, sequence, semantic_sql, status,
                 row_limit, returned_row_count, result_truncated,
                 result, started_at)
                VALUES (%s, %s, 1, 'SELECT 1', 'running', 100, 0, false,
                        '{"columns":[],"rows":[]}', now())""",
                (uuid4(), request_id),
            )


async def test_attempts_are_read_in_sequence_order(audit_repo, request_id):
    # Insert sequences 2 then 1, terminalize both, and assert [1, 2].
    audit = await audit_repo.get_canonical_audit(request_id)
    assert [item.sequence for item in audit.sql_attempts] == [1, 2]
```

- [ ] **Step 3: Run integration tests and verify repository imports fail**

Run: `pytest tests/integration/test_migrations.py tests/integration/test_audit_repository.py -v`

Expected: FAIL because `db.py`, `audit.py`, and the migration do not exist.

- [ ] **Step 4: Implement separate pool factories and migration runner**

The application pool uses default transaction behavior and `dict_row`. The checkpoint pool is separate and uses:

```python
kwargs={
    "autocommit": True,
    "prepare_threshold": 0,
    "row_factory": dict_row,
}
```

Never run audit repository calls through the checkpoint pool.

- [ ] **Step 5: Implement audit state transitions with conditional updates**

`start_attempt()` obtains the next sequence in one short transaction by locking the parent request row, counting existing attempts, enforcing the maximum of 3, and inserting `running`. `set_executed_sql`, `succeed_attempt`, and `fail_attempt` update with `WHERE attempt_id=%s AND status='running'` and require `rowcount == 1`; otherwise raise `AttemptAlreadyTerminal`.

Use `psycopg.types.json.Jsonb` for JSONB values. `get_canonical_audit()` returns `session_id`, `question`, `answer`, and attempts ordered by sequence; it does not expose this through HTTP.

- [ ] **Step 6: Run integration tests**

Run: `pytest tests/integration/test_migrations.py tests/integration/test_audit_repository.py -v`

Expected: PASS and no connections remain checked out after the suite.

- [ ] **Step 7: Commit**

```powershell
git add services/wren-chat-api/migrations services/wren-chat-api/src/wren_chat_api/db.py services/wren-chat-api/src/wren_chat_api/audit.py services/wren-chat-api/tests/integration
git commit -m "feat: persist Wren chat audit trails"
```

---

### Task 4: Implement Session Leases and Interrupted Attempt Recovery

**Files:**
- Create: `services/wren-chat-api/src/wren_chat_api/leases.py`
- Create: `services/wren-chat-api/src/wren_chat_api/recovery.py`
- Create: `services/wren-chat-api/tests/integration/test_leases.py`
- Create: `services/wren-chat-api/tests/integration/test_recovery.py`

**Interfaces:**
- Produces: `LeaseRepository.acquire(session_id, ttl) -> Lease | None`, `renew(lease, ttl) -> bool`, and `release(lease) -> bool`.
- Produces: `recover_interrupted(pool, now, threshold) -> RecoveryCounts`.
- Produces: `run_recovery_loop(stop_event, interval_seconds, ...) -> None`.

- [ ] **Step 1: Write failing atomic lease tests**

```python
async def test_only_one_live_lease_can_be_acquired(leases):
    first = await leases.acquire("same-session", ttl=timedelta(seconds=30))
    second = await leases.acquire("same-session", ttl=timedelta(seconds=30))
    assert first is not None
    assert second is None


async def test_expired_lease_can_be_replaced(leases, clock):
    first = await leases.acquire("s-1", ttl=timedelta(seconds=1))
    clock.advance(seconds=2)
    second = await leases.acquire("s-1", ttl=timedelta(seconds=30))
    assert second is not None
    assert second.lease_id != first.lease_id
```

- [ ] **Step 2: Implement leases with one upsert statement**

Use `INSERT ... ON CONFLICT (session_id) DO UPDATE ... WHERE chat_session_leases.expires_at <= now()` and return the new row. Renewal and release must match both `session_id` and `lease_id`, preventing an expired holder from modifying a replacement lease.

- [ ] **Step 3: Write failing recovery tests for both interruption phases**

Create one stale attempt with `executed_sql=NULL` and one with executed SQL. Expire their leases, run recovery, and assert:

```python
assert planning.error["code"] == "SQL_ATTEMPT_INTERRUPTED"
assert planning.error["phase"] == "SQL_PLANNING"
assert execution.error["phase"] == "SQL_EXECUTION"
assert execution.error["metadata"] == {"outcome": "unknown"}
assert request.status == "failed"
assert request.error["code"] == "REQUEST_INTERRUPTED"
```

Also prove an old attempt with a live lease is untouched and that running recovery twice changes no terminal rows.

- [ ] **Step 4: Implement idempotent recovery in one transaction**

Use conditional updates on `status='running'`, join through the request's `session_id`, require an absent/expired lease, and calculate `duration_ms` from `started_at` to the recovery timestamp. Recover attempts before parent requests.

- [ ] **Step 5: Run lease and recovery integration tests**

Run: `pytest tests/integration/test_leases.py tests/integration/test_recovery.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add services/wren-chat-api/src/wren_chat_api/leases.py services/wren-chat-api/src/wren_chat_api/recovery.py services/wren-chat-api/tests/integration/test_leases.py services/wren-chat-api/tests/integration/test_recovery.py
git commit -m "feat: recover interrupted chat queries"
```

---

### Task 5: Enforce Read-Only SQL and Normalize Bounded Results

**Files:**
- Create: `services/wren-chat-api/src/wren_chat_api/sql_policy.py`
- Create: `services/wren-chat-api/src/wren_chat_api/results.py`
- Create: `services/wren-chat-api/tests/unit/test_sql_policy.py`
- Create: `services/wren-chat-api/tests/unit/test_results.py`

**Interfaces:**
- Produces: `validate_read_only_sql(sql: str, dialect: str) -> None`.
- Produces: `build_attempt_result(table: pa.Table, row_limit: int, max_bytes: int) -> NormalizedResult`.
- Produces: `build_tool_content(result: NormalizedResult, max_bytes: int) -> ToolContent`, where `ToolContent.content_truncated` is independent of the audit row's `result_truncated`.
- Produces: `normalize_json(value: Any) -> Any` and `redact_secrets(value: Any) -> Any`.

- [ ] **Step 1: Write failing SQL-policy parameter tests**

Include allowed single queries and rejected write/command/multiple statements:

```python
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM orders",
    "WITH x AS (SELECT * FROM orders) SELECT COUNT(*) FROM x",
    "SELECT region FROM orders UNION ALL SELECT region FROM returns",
])
def test_read_only_queries_are_allowed(sql):
    validate_read_only_sql(sql, dialect="postgres")


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE orders SET status='x'",
    "INSERT INTO orders VALUES (1)",
    "DROP TABLE orders",
    "BEGIN",
    "SELECT 1; SELECT 2",
])
def test_non_read_only_or_multiple_statements_are_rejected(sql):
    with pytest.raises(ReadOnlySqlRequired):
        validate_read_only_sql(sql, dialect="postgres")
```

- [ ] **Step 2: Implement validation using sqlglot ASTs**

Parse with `sqlglot.parse(sql, dialect=dialect)`, require exactly one expression, require a query expression, and recursively reject write/DDL/command/transaction nodes. Do not rely on string prefixes.

- [ ] **Step 3: Write failing result-probe and normalization tests**

```python
def test_n_plus_one_row_is_removed_and_marks_truncated():
    table = pa.table({"id": [1, 2, 3]})
    result = build_attempt_result(table, row_limit=2, max_bytes=10_000)
    assert result.returned_row_count == 2
    assert result.result_truncated is True
    assert result.result["rows"] == [{"id": 1}, {"id": 2}]


def test_decimal_is_string_and_nonfinite_float_is_null():
    normalized = normalize_json(
        {"amount": Decimal("1.20"), "ratio": float("nan")}
    )
    assert normalized == {"amount": "1.20", "ratio": None}


def test_oversized_single_row_raises_result_too_large():
    table = pa.table({"payload": ["x" * 2_000]})
    with pytest.raises(ResultTooLarge):
        build_attempt_result(table, row_limit=100, max_bytes=1_024)


def test_tool_content_cap_does_not_change_audit_truncation():
    table = pa.table({"payload": ["x" * 1_000, "y" * 1_000]})
    result = build_attempt_result(table, row_limit=100, max_bytes=10_000)
    tool_content = build_tool_content(result, max_bytes=512)
    assert result.returned_row_count == 2
    assert result.result_truncated is False
    assert tool_content.content_truncated is True
```

- [ ] **Step 4: Implement byte-bounded JSON output**

Normalize the first `row_limit` rows, calculate truncation from `table.num_rows > row_limit`, serialize the final `{columns, rows}` once with compact JSON separators, and reject audit payloads above `max_bytes`. Never silently sample the audit result below `row_limit`; an oversized audit payload becomes a structured failed attempt so the model can narrow and retry.

Build LLM-facing content separately: include column names, truncation flags, and as many leading normalized rows as fit within `max_tool_content_bytes`, followed by an explicit instruction to aggregate, filter, or select fewer columns when tool content is cut. The `ToolMessage` must contain only this bounded content and scalar metadata; it must not serialize the full audit `result` a second time.

- [ ] **Step 5: Run focused tests and Ruff**

Run: `pytest tests/unit/test_sql_policy.py tests/unit/test_results.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add services/wren-chat-api/src/wren_chat_api/sql_policy.py services/wren-chat-api/src/wren_chat_api/results.py services/wren-chat-api/tests/unit/test_sql_policy.py services/wren-chat-api/tests/unit/test_results.py
git commit -m "feat: bound read-only chat query results"
```

---

### Task 6: Build the Pre-Execution Audited Wren Query Component

**Files:**
- Create: `services/wren-chat-api/src/wren_chat_api/executor.py`
- Create: `services/wren-chat-api/src/wren_chat_api/audited_query.py`
- Create: `services/wren-chat-api/tests/unit/test_executor.py`
- Create: `services/wren-chat-api/tests/unit/test_audited_query.py`

**Interfaces:**
- Consumes: `WrenToolkit.plan_query`, `WrenToolkit.execute_planned`, `AuditRepository`, result normalization, and SQL policy.
- Produces: `BoundedWrenExecutor.run(callable, *args)`, which admits at most `wren_workers + wren_queue_capacity` live/queued calls and retains capacity until the actual blocking future finishes.
- Produces: `AuditedQuery.execute(context: RunContext, sql: str, limit: int = 100) -> dict[str, Any]`.
- Produces: an LLM-facing tool named `wren_query` whose schema exposes only `sql` and `limit`.

- [ ] **Step 1: Write failing order-of-operations tests**

Use an event list to prove audit writes surround Wren calls:

```python
async def test_running_attempt_and_executed_sql_are_persisted_before_query():
    events = []
    audit = FakeAudit(events)
    toolkit = FakeToolkit(events, rows=[{"value": 1}])
    component = AuditedQuery(audit=audit, toolkit=toolkit, settings=settings)

    result = await component.execute(context, "SELECT 1", limit=100)

    assert result["ok"] is True
    assert events == [
        "audit:start_attempt",
        "wren:plan",
        "audit:set_executed_sql",
        "wren:execute_planned:101",
        "audit:succeed_attempt",
    ]
```

Add tests proving planning failure records `executed_sql=None`, execution failure retains executed SQL, and failure of `start_attempt()` leaves both toolkit call counts at zero.

Also prove that failure of `set_executed_sql()`, `succeed_attempt()`, or `fail_attempt()` is raised as `PersistenceFailed` rather than returned to the model as an ordinary retryable SQL error. Audit persistence failure must stop the request.

- [ ] **Step 2: Write failing bounded-executor tests**

Block one worker and fill the configured queue; the next submission must fail immediately with `CapacityExceeded`. Cancel an awaiting coroutine and prove its admission token is not returned until the underlying thread future actually completes. This prevents timed-out database work from becoming invisible to the overload guard.

- [ ] **Step 3: Run the focused tests and verify the components are absent**

Run: `pytest tests/unit/test_executor.py tests/unit/test_audited_query.py -v`

Expected: FAIL on import.

- [ ] **Step 4: Implement bounded blocking-call submission**

Wrap `ThreadPoolExecutor(max_workers=wren_workers)` with an admission counter capped at `wren_workers + wren_queue_capacity`. Reserve capacity before `executor.submit()`; if full, raise `CapacityExceeded` without submitting. Attach a `Future.add_done_callback()` that releases capacity only when the real thread work completes. Await the future through `asyncio.wrap_future()` plus `asyncio.shield()` so coroutine cancellation does not cancel the accounting future or release its slot early. Shutdown stops new admission and waits for active calls according to the lifespan policy.

- [ ] **Step 5: Implement two-phase execution on the bounded Wren executor**

Use `BoundedWrenExecutor.run(...)` for both synchronous Wren phases. The sequence is fixed:

```python
attempt = await audit.start_attempt(request_id, sql, row_limit)
try:
    validate_read_only_sql(sql, dialect)
    plan = await run_wren(toolkit.plan_query, sql)
    await audit.set_executed_sql(attempt.attempt_id, plan.dialect_sql)
    table = await run_wren(toolkit.execute_planned, plan, row_limit + 1)
    normalized = build_attempt_result(table, row_limit, max_result_bytes)
    await audit.succeed_attempt(attempt.attempt_id, normalized)
    return success_envelope(
        build_tool_content(normalized, max_tool_content_bytes)
    )
except Exception as exc:
    error = structure_and_redact_error(exc)
    await audit.fail_attempt(attempt.attempt_id, error)
    return error_envelope(error)
```

Do not call `WrenToolkit.query()`. Do not repeat `plan_query()` after `executed_sql` is persisted. Catch `asyncio.CancelledError` separately and re-raise it without terminalizing the attempt; the recovery worker later records `SQL_ATTEMPT_INTERRUPTED` because the actual planning/query outcome is unknown.

The success envelope sent to the graph contains the bounded text plus scalar fields (`returned_row_count`, audit `result_truncated`, and `content_truncated`), not the full persisted `rows` object. Structured SQL failures may be returned to the model for correction, but any audit repository failure is re-raised as `PersistenceFailed` and must terminate the whole request.

- [ ] **Step 6: Enforce the three-attempt budget in `start_attempt()`**

When the repository reports `SqlAttemptLimitReached`, return an error envelope with `SQL_RETRY_EXHAUSTED` and do not invoke Wren. This rejected fourth call is not inserted as a database SQL attempt because no SQL is permitted to reach planning or execution.

- [ ] **Step 7: Run executor and audited-query tests**

Run: `pytest tests/unit/test_executor.py tests/unit/test_audited_query.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add services/wren-chat-api/src/wren_chat_api/executor.py services/wren-chat-api/src/wren_chat_api/audited_query.py services/wren-chat-api/tests/unit/test_executor.py services/wren-chat-api/tests/unit/test_audited_query.py
git commit -m "feat: audit SQL before Wren execution"
```

---

### Task 7: Build a Sequential LangGraph Agent and Compact Conversation State

**Files:**
- Create: `services/wren-chat-api/src/wren_chat_api/agent.py`
- Create: `services/wren-chat-api/tests/unit/test_agent.py`
- Create: `services/wren-chat-api/tests/integration/test_agent_checkpoint.py`

**Interfaces:**
- Produces: `RunContext(request_id: UUID, session_id: str, audited_query: AuditedQuery)`.
- Produces: `ChatState(messages: Annotated[list[AnyMessage], add_messages], summary: str)`.
- Produces: `build_chat_graph(toolkit, model, summarizer, checkpointer, settings) -> CompiledStateGraph`.
- Produces: `invoke_chat(graph, thread_id, question, context, settings) -> str` with synchronous checkpoint durability.

- [ ] **Step 1: Write a failing graph test for sequential SQL calls**

Use a deterministic fake chat model that emits two SQL tool calls in one AI message. Assert the custom tool node calls `AuditedQuery.execute()` in the model-provided order, never concurrently, and returns tool messages in that order.

```python
assert audited.calls == ["SELECT 1", "SELECT 2"]
assert audited.max_in_flight == 1
```

- [ ] **Step 2: Implement a custom sequential tool node**

Bind these tools to the model:

- schema/memory-read tools from `toolkit.get_tools(include_memory_write=False)` except the ordinary `wren_query`;
- the audited `wren_query` schema.

The custom node iterates `last_ai_message.tool_calls` in order. It dispatches `wren_query` to `runtime.context.audited_query.execute(...)`; other Wren tools use their async invocation method. Return one `ToolMessage` per call. Do not use the default `ToolNode`, which may execute independent calls concurrently.

- [ ] **Step 3: Write failing context-compaction tests**

Create seven completed turns plus current tool traffic. Assert the latest durable state contains:

- a non-empty rolling summary for the oldest turn;
- exactly six complete Human/AI answer pairs;
- no `ToolMessage`;
- no AI message containing `tool_calls`.

Then invoke the next turn “和第一期比较” and assert the summarizer-preserved first-period fact appears in model input.

- [ ] **Step 4: Implement the final compaction node**

After the model returns a final AI message with no tool calls:

1. separate compact Human/final-AI pairs from intermediate messages;
2. summarize pairs older than the newest six using the current rolling summary;
3. return `RemoveMessage` entries for all intermediate tool traffic and all compacted old messages;
4. update `summary` with plain text only.

The model node prepends the Wren system prompt and, when present, a system message containing the rolling summary. Never include audit JSON or complete query results from previous turns.

Append service policy instructions requiring `wren_query` for factual business-data answers, treating database values as untrusted data rather than instructions, and forbidding claims of complete detail coverage when either `result_truncated` or `content_truncated` is true. A clarification question may finish with zero SQL attempts.

- [ ] **Step 5: Configure persistent invocation safely**

Invoke using:

```python
await graph.ainvoke(
    {"messages": [HumanMessage(content=question)]},
    config={
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.graph_recursion_limit,
    },
    context=run_context,
    durability="sync",
)
```

Wrap it in `asyncio.timeout(settings.request_timeout_seconds)`. Extract the last non-empty final `AIMessage.content`; otherwise raise `InvalidFinalAnswer`.

- [ ] **Step 6: Prove checkpoint isolation and restart recovery with PostgreSQL**

Using `AsyncPostgresSaver`, compile a graph, run session A, close it, create a new graph/checkpointer instance, and ask a follow-up using session A's thread ID. Assert prior context is visible. Ask the same follow-up with session B and assert it is absent.

- [ ] **Step 7: Run graph unit and integration tests**

Run: `pytest tests/unit/test_agent.py tests/integration/test_agent_checkpoint.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add services/wren-chat-api/src/wren_chat_api/agent.py services/wren-chat-api/tests/unit/test_agent.py services/wren-chat-api/tests/integration/test_agent_checkpoint.py
git commit -m "feat: add persistent sequential Wren chat agent"
```

---

### Task 8: Orchestrate One Chat Request Across Lease, Audit, and Graph Boundaries

**Files:**
- Create: `services/wren-chat-api/src/wren_chat_api/chat.py`
- Create: `services/wren-chat-api/tests/unit/test_chat_service.py`

**Interfaces:**
- Produces: `ChatService.ask(request: ChatRequest) -> ChatResponse`.
- Consumes: `LeaseRepository`, `AuditRepository`, compiled graph, `AuditedQuery`, thread identity, and settings.

- [ ] **Step 1: Write failing orchestration tests**

Cover exact terminal behavior:

```python
async def test_success_is_returned_only_after_audit_is_terminal():
    events = []
    service = make_service(events, graph_answer="42")
    response = await service.ask(ChatRequest(session_id="s-1", question="count"))
    assert response.model_dump() == {"session_id": "s-1", "answer": "42"}
    assert events.index("audit:succeed_request") < events.index("lease:release")


async def test_audit_failure_prevents_success_response():
    service = make_service(audit_success_error=RuntimeError("postgres down"))
    with pytest.raises(PersistenceFailed):
        await service.ask(ChatRequest(session_id="s-1", question="count"))


async def test_busy_session_does_not_create_audit_request():
    service = make_service(lease=None)
    with pytest.raises(SessionBusy):
        await service.ask(ChatRequest(session_id="s-1", question="count"))
    assert service.audit.start_request.call_count == 0
```

- [ ] **Step 2: Implement lease renewal as a sibling task**

After acquisition, run a renewal loop every 10 seconds. If renewal fails or ownership is lost, cancel graph work and fail the request with `SESSION_LEASE_LOST`. Always attempt release in `finally`, but never let a release error overwrite an already-recorded primary failure.

- [ ] **Step 3: Implement request state transitions**

Order:

```text
acquire lease
insert running request
invoke graph with derived thread ID and RunContext
mark request succeeded with answer
release lease
return ChatResponse
```

On any exception after `start_request`, convert it to a redacted structured request error and conditionally call `fail_request`. If failure persistence itself fails, raise `PersistenceFailed` and emit a critical structured application log.

- [ ] **Step 4: Run orchestration tests**

Run: `pytest tests/unit/test_chat_service.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add services/wren-chat-api/src/wren_chat_api/chat.py services/wren-chat-api/tests/unit/test_chat_service.py
git commit -m "feat: orchestrate session chat requests"
```

---

### Task 9: Add FastAPI Lifespan, Authentication, Routes, Health, and Metrics

**Files:**
- Create: `services/wren-chat-api/src/wren_chat_api/auth.py`
- Create: `services/wren-chat-api/src/wren_chat_api/metrics.py`
- Create: `services/wren-chat-api/src/wren_chat_api/app.py`
- Create: `services/wren-chat-api/src/wren_chat_api/main.py`
- Create: `services/wren-chat-api/tests/unit/test_auth.py`
- Create: `services/wren-chat-api/tests/api/test_chat_api.py`
- Create: `services/wren-chat-api/tests/api/test_health.py`

**Interfaces:**
- Produces: `create_app(settings: Settings | None = None, overrides: AppOverrides | None = None) -> FastAPI`.
- Produces: `POST /v1/chat`, `GET /health/live`, `GET /health/ready`, and `GET /metrics`.
- Consumes: `ChatService.ask`.

- [ ] **Step 1: Write failing API contract tests**

```python
async def test_chat_success_has_exactly_two_fields(client, fake_chat_service):
    response = await client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer test-key"},
        json={"session_id": "s-1", "question": "count orders"},
    )
    assert response.status_code == 200
    assert response.json() == {"session_id": "s-1", "answer": "42"}


async def test_invalid_key_returns_generic_401(client):
    response = await client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer wrong"},
        json={"session_id": "s-1", "question": "count orders"},
    )
    assert response.status_code == 401
    assert "wrong" not in response.text


async def test_unknown_request_field_is_rejected(client):
    response = await client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer test-key"},
        json={"session_id": "s-1", "question": "count", "sql": "SELECT 1"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
```

- [ ] **Step 2: Implement constant-time bearer authentication**

Use `secrets.compare_digest()` against `settings.api_key.get_secret_value()`. Do not log the header or key. Health liveness is unauthenticated; readiness, chat, and metrics follow deployment policy: chat always requires the service key, metrics binds only to the internal service network documented in Compose/README.

- [ ] **Step 3: Implement lifespan resources**

During startup:

1. create and open the application pool;
2. apply application migrations;
3. create and open the separate checkpoint pool;
4. instantiate `AsyncPostgresSaver(checkpoint_pool)` and `await setup()`;
5. initialize `WrenToolkit.from_project(project_path, config=WrenConfig(strict_mode=True))`; strict mode's built-in policy blocks non-MDL tables and file/network reader functions, while the business database credential remains read-only;
6. create `BoundedWrenExecutor(max_workers=settings.wren_workers, queue_capacity=settings.wren_queue_capacity)`;
7. initialize the model, graph, repositories, and chat service;
8. run one recovery pass and start the recovery loop.

During shutdown, stop recovery, cancel/await background tasks, close the Wren connector, shut down the executor, and close both pools.

- [ ] **Step 4: Add routes and exception mapping**

`POST /v1/chat` delegates once to `ChatService.ask`. Install a `RequestValidationError` handler so malformed JSON, missing/invalid fields, and unknown fields return HTTP `400` with `INVALID_REQUEST`; do not expose Pydantic input values. Map authentication to `401`, same-session conflict to `409`, a well-formed but unanswerable question to `422`, capacity to `429`, persistence/internal-state failure to `500`, model/business-database failure to `502`, and timeout to `504`. Every failure returns only the stable `ErrorResponse`; never return exception strings.

Readiness executes `SELECT 1` through the application pool and verifies that the already-initialized toolkit remains available; it must not call the LLM or business database.

- [ ] **Step 5: Add low-cardinality metrics**

Expose counters/histograms for requests, durations, SQL attempts/status, truncation, lease conflicts, recovery counts, pool saturation, and persistence failures. Labels may include route, terminal status, and stable error code. Never label with `session_id`, `request_id`, question, SQL, answer, or database value.

- [ ] **Step 6: Run API tests**

Run: `pytest tests/unit/test_auth.py tests/api/test_chat_api.py tests/api/test_health.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add services/wren-chat-api/src/wren_chat_api/auth.py services/wren-chat-api/src/wren_chat_api/metrics.py services/wren-chat-api/src/wren_chat_api/app.py services/wren-chat-api/src/wren_chat_api/main.py services/wren-chat-api/tests/unit/test_auth.py services/wren-chat-api/tests/api
git commit -m "feat: expose Wren chat HTTP API"
```

---

### Task 10: Prove the Complete Audit and Multi-Turn Flow End to End

**Files:**
- Create: `services/wren-chat-api/tests/e2e/test_chat_flow.py`
- Create: `services/wren-chat-api/tests/e2e/fixtures/wren_project/wren_project.yml`
- Create: `services/wren-chat-api/tests/e2e/fixtures/wren_project/models/orders.yml`
- Create: `services/wren-chat-api/tests/e2e/fixtures/wren_project/target/mdl.json`
- Create: `services/wren-chat-api/tests/e2e/fixtures/wren_project/orders.duckdb` through a pytest fixture, not as a committed binary.

**Interfaces:**
- Consumes: the assembled FastAPI app, real local Wren planning/execution against DuckDB, PostgreSQL audit/checkpoint storage, and a deterministic fake tool-calling model.
- Produces: evidence for the core acceptance criteria without an external LLM API or business database service.

- [ ] **Step 1: Create a deterministic DuckDB Wren project fixture**

At test setup, create an `orders` table with more than 100 detail rows and known totals. Build or provide a valid MDL that maps the `orders` model to that table. The fixture's database profile must be read-only for execution tests where the connector supports it.

- [ ] **Step 2: Write the failing correction-chain end-to-end test**

The fake model must:

1. emit a query with a nonexistent `sales_amount` column;
2. read the structured tool error;
3. emit corrected `SUM(amount)` SQL;
4. return a natural-language answer.

Assert the public response contains only `session_id` and `answer`. Query PostgreSQL directly and assert two attempts ordered by sequence, the first failed with `executed_sql=NULL`, and the second succeeded with one row and the correct decimal string.

- [ ] **Step 3: Add result-completeness tests**

Ask for detail rows and assert `returned_row_count=100` plus `result_truncated=true`. Ask for `SUM(amount)` and assert the answer/result uses all fixture rows even though the result row limit remains 100.

- [ ] **Step 4: Add multi-turn isolation and restart tests**

Run a first question in session A, reconstruct the app to simulate restart, then ask “那去年呢？” with session A and prove the earlier period is available. Ask the same in session B and prove it is not. Also issue two concurrent calls for one session and assert one is `409`, while two distinct sessions both proceed.

- [ ] **Step 5: Add crash-window recovery test**

Inject a barrier after `set_executed_sql()` but before `execute_planned()`, cancel the request task, expire the lease, run recovery, and assert the attempt becomes failed with `SQL_ATTEMPT_INTERRUPTED`, `phase=SQL_EXECUTION`, and `outcome=unknown`. Repeat with cancellation before planning and assert `phase=SQL_PLANNING`.

- [ ] **Step 6: Run the complete service suite**

Run:

```powershell
Set-Location services/wren-chat-api
pytest -v
ruff check src tests
ruff format --check src tests
python -m build
```

Expected: all tests PASS, Ruff exits 0, and sdist/wheel build successfully.

- [ ] **Step 7: Commit**

```powershell
git add services/wren-chat-api/tests/e2e
git commit -m "test: verify Wren chat audit lifecycle"
```

---

### Task 11: Add Container Packaging, Operator Documentation, and CI

**Files:**
- Create: `services/wren-chat-api/Dockerfile`
- Create: `services/wren-chat-api/compose.yml`
- Create: `services/wren-chat-api/.env.example`
- Create: `services/wren-chat-api/README.md`
- Create: `.github/workflows/wren-chat-api-ci.yml`

**Interfaces:**
- Produces: a container started with `uvicorn wren_chat_api.main:app --host 0.0.0.0 --port 8080`.
- Produces: local Compose services `postgres` and `wren-chat-api` with health checks.
- Documents: API request/response, configuration, audit SQL, `docker compose logs`, retention, recovery semantics, and security requirements.

- [ ] **Step 1: Write the Dockerfile using repository-root build context**

The image must install local `core/wren`, `sdk/wren-langchain`, and the service package so the newly added planned-query API is present. Run as a non-root user, expose port 8080, and use the liveness endpoint for the container health check. Do not copy `.env` or profile secrets into the image.

- [ ] **Step 2: Add local Compose without pretending it is production**

Define PostgreSQL 15+ with a named volume and the API with environment variables loaded from a developer-created `.env`. Mount a sample Wren project path read-only. Bind PostgreSQL to the Compose network only; do not publish its port by default.

- [ ] **Step 3: Document exact log-inspection workflows**

Include:

```sql
SELECT request_id, session_id, question, answer, status, error,
       started_at, completed_at
FROM chat_audit_requests
WHERE session_id = 'session-001'
ORDER BY started_at DESC;
```

and:

```sql
SELECT sequence, semantic_sql, executed_sql, status, row_limit,
       returned_row_count, result_truncated, result, error,
       duration_ms, started_at, completed_at
FROM chat_sql_attempts
WHERE request_id = '<request UUID>'
ORDER BY sequence ASC;
```

Also document `docker compose logs -f wren-chat-api`, explain that `running` attempts can become `SQL_ATTEMPT_INTERRUPTED`, and state that audit rows may contain sensitive business results requiring an explicit retention policy.

- [ ] **Step 4: Add path-filtered CI**

CI jobs:

- Ruff lint/format on Python 3.11;
- unit/API tests on Python 3.11 and 3.12;
- PostgreSQL integration/e2e tests using a PostgreSQL service container;
- package build;
- Docker build from repository root.

Trigger on `services/wren-chat-api/**`, `core/wren/**`, `sdk/wren-langchain/**`, and the workflow file.

- [ ] **Step 5: Run final local verification**

Run:

```powershell
Set-Location core/wren
uv run --no-sync pytest tests/unit/test_engine.py -v
Set-Location ../../sdk/wren-langchain
pytest tests/unit/test_toolkit_runtime.py tests/unit/test_tools_runtime.py -v
Set-Location ../../services/wren-chat-api
pytest -v
ruff check src tests
ruff format --check src tests
python -m build
Set-Location ../..
docker build -f services/wren-chat-api/Dockerfile .
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 6: Review against the Contribution Bar**

Confirm the change description can point to executed tests for every behavior claim, that tests call real functions rather than inspect source text, and that the new workflow actually runs the service tests in CI.

- [ ] **Step 7: Commit**

```powershell
git add services/wren-chat-api/Dockerfile services/wren-chat-api/compose.yml services/wren-chat-api/.env.example services/wren-chat-api/README.md .github/workflows/wren-chat-api-ci.yml
git commit -m "chore: package Wren chat API service"
```

---

## Completion Gate

Before declaring the feature complete:

1. Run all verification commands from Task 11 with fresh output.
2. Inspect `git status --short` and confirm only intentional changes exist.
3. Query a real test request's audit rows and verify the SQL chain is ordered by `sequence`, not timestamps.
4. Kill a request between planned-SQL persistence and query completion; verify recovery produces `SQL_ATTEMPT_INTERRUPTED` rather than losing the attempt.
5. Verify a public success payload contains no SQL, results, request ID, trace ID, or extra keys.
6. Verify an error response and structured application logs contain no API key, database password, connection URL, raw session ID, or result rows.
7. Read the repository's Contribution Bar again before opening a pull request.
