# Session-Isolated Wren Chat API Design

**Date:** 2026-08-12

**Status:** Approved design sections consolidated for final review

## 1. Objective

Add a separately deployable HTTP service that lets a backend application ask
natural-language questions about a Wren project. The service must:

- accept a caller-provided `session_id` and `question`;
- preserve multi-turn context for the same `session_id`;
- isolate different `session_id` values from each other;
- let an LLM use Wren to generate and execute read-only analytical SQL;
- return only `session_id` and the final natural-language `answer`;
- persist every SQL attempt, result, structured error, and correction in
  execution order for internal audit and troubleshooting.

The service has no tenant or user concept. `session_id` is the only
conversation identity supplied by the caller.

## 2. Scope

### 2.1 Included in the first release

- One blocking JSON chat endpoint.
- Persistent multi-turn conversation state.
- Strict isolation by `session_id`.
- Wren-backed schema discovery, SQL planning, and query execution.
- Audit logging for the complete SQL retry chain.
- PostgreSQL-backed conversation checkpoints, audit data, and session leases.
- Read-only SQL enforcement, execution limits, context limits, and timeouts.
- Health endpoints, structured application logs, and service metrics.

### 2.2 Excluded from the first release

- Streaming or Server-Sent Events.
- A chat UI or audit-log UI.
- Tenant-level or user-level authorization boundaries.
- Returning SQL or structured query results to the API caller.
- Allowing callers to choose arbitrary Wren projects or database profiles.
- Long-term user memory across different session IDs.
- Database writes or automatic writes to Wren's shared NL-to-SQL memory.

## 3. Open-Source Foundation and Placement

The service will use:

- **FastAPI** for the HTTP and OpenAPI boundary.
- **LangGraph** for the agent loop and thread-level persistence.
- **LangGraph PostgreSQL Checkpointer** for persistent conversation state.
- **Wren LangChain SDK** (`sdk/wren-langchain`) for Wren-aware tools and the
  project system prompt.
- **Wren Core Python SDK** (`core/wren`) for semantic SQL planning and query
  execution.
- **PostgreSQL** for checkpoints, audit records, and session leases.

The new module will live at `services/wren-chat-api/`. Keeping the service
separate prevents the base `wrenai` package from acquiring an LLM server
runtime while still reusing the repository's existing SDKs.

Full platforms such as Dify and Flowise are not core dependencies. Their
conversation APIs satisfy the general use case, but they would add a second
application platform and duplicate capabilities already available through
the repository's LangGraph integration.

## 4. High-Level Architecture

```text
Backend application
    |
    | POST /v1/chat {session_id, question}
    v
FastAPI boundary
    |-- request validation
    |-- service API authentication
    |-- session lease acquisition
    |-- audit request creation
    v
LangGraph Wren agent
    |-- persistent state keyed by internal thread_id
    |-- Wren context and read-only tools
    |-- audited SQL tool
    v
Wren semantic layer --> read-only business database
    |
    +--> PostgreSQL SQL-attempt audit rows

PostgreSQL also stores LangGraph checkpoints and session leases.
```

The API, conversation state, and audit store are separate boundaries:

- The API exposes only the final answer.
- The checkpoint store contains the compact conversation state needed for
  later turns.
- The audit tables contain detailed execution evidence and are not injected
  into later LLM context.

## 5. HTTP Contract

### 5.1 Chat endpoint

```http
POST /v1/chat
Authorization: Bearer <service-api-key>
Content-Type: application/json
```

Request:

```json
{
  "session_id": "5d3b168f-5dc3-4c6b-9239-bbb0edbb25c7",
  "question": "上个月销售额是多少？"
}
```

Success response:

```json
{
  "session_id": "5d3b168f-5dc3-4c6b-9239-bbb0edbb25c7",
  "answer": "上个月销售额为 128 万元。"
}
```

The success response contains exactly two fields: `session_id` and `answer`.
It never returns generated SQL, query results, retry details, internal request
IDs, trace IDs, or model reasoning.

If a question needs clarification, the service may return a clarification in
`answer` without executing SQL. Such a completed turn is successful and has
an empty SQL-attempt list.

### 5.2 Validation

- `session_id` is required, non-blank, at most 128 characters, and must match
  `^[A-Za-z0-9._:-]+$`. UUID values are recommended but not required.
- `question` is required, non-blank after trimming, and at most 4,000
  characters.
- Unknown request fields are rejected.
- Callers must reuse the exact same `session_id` to continue a conversation.
- If two callers reuse one `session_id`, the service treats them as the same
  conversation because no additional identity dimension exists.

### 5.3 Error response

Errors use a stable envelope distinct from the success schema:

```json
{
  "error": {
    "code": "CHAT_EXECUTION_FAILED",
    "message": "Unable to complete the data question."
  }
}
```

The public message must not contain SQL, database driver details, connection
information, prompts, or secrets. Detailed structured errors remain in the
audit store.

Status mapping:

- `400`: invalid request data;
- `401`: missing or invalid service API key;
- `409`: another request is running for the same session;
- `422`: the question could not be converted into a valid data answer;
- `429`: service concurrency or rate limit exceeded;
- `500`: internal state or audit persistence failure;
- `502`: upstream model or business database failure;
- `504`: total request timeout.

### 5.4 Health endpoints

- `GET /health/live` reports that the process is running.
- `GET /health/ready` verifies PostgreSQL connectivity and successful
  initialization of the configured Wren project. It does not run a business
  query or call the LLM.

## 6. Conversation Identity and Isolation

The service derives a fixed internal identifier:

```text
thread_id = "wren-chat:" + SHA-256(UTF-8(session_id))
```

Hashing keeps raw external IDs out of LangGraph checkpoint keys without
introducing a secret whose rotation would break existing sessions. The raw
`session_id` remains in audit records for operational lookup.

All of the following use the same conversation boundary:

- LangGraph checkpoint reads and writes;
- session lease acquisition;
- context compaction;
- internal audit lookup.

No query may load a checkpoint by a different session ID. Different sessions
may execute concurrently.

## 7. Agent and Wren Integration

### 7.1 Agent topology

The graph uses the standard Wren-aware ReAct loop:

```text
START -> model -> tool routing -> tools -> model -> END
```

`WrenToolkit.from_project()` supplies the configured Wren project, schema
tools, memory-read tools when available, and the Wren-aware system prompt.
`wren_store_query` is not exposed, so ordinary chat traffic cannot modify the
project's shared NL-to-SQL memory.

The model may inspect schema and recall curated query examples before
executing SQL. Only the audited SQL tool is allowed to query the business
database.

### 7.2 Audited SQL tool

The service provides `audited_wren_query`, replacing the ordinary
LLM-facing `wren_query` tool for this agent. For each call it:

1. reserves the next request-local sequence number;
2. inserts an attempt with `status=running`, the start time, the
   model-provided semantic SQL, and the row limit;
3. validates the limit and read-only SQL policy;
4. asks Wren to produce target-dialect SQL;
5. persists `executed_sql` on the still-running attempt before sending that
   SQL to the business database;
6. executes the planned SQL once with an `N+1` result probe;
7. normalizes and bounds the result;
8. atomically changes the attempt to `success` with its result or `failed`
   with its structured error;
9. returns the result or error envelope to the model.

Every tool call is an attempt, including planning failures. A later corrected
query creates a new attempt with the next sequence value. If the initial
`running` insert cannot be committed, the tool must not plan or execute SQL.

### 7.3 Capturing the executed SQL without duplicate execution

`WrenEngine.query()` currently plans internally and returns only a PyArrow
table. The implementation will add a public Wren Core SDK operation that
returns both the planned target-dialect SQL and the result table from one
logical execution. The existing `query()` method remains backward-compatible
and delegates to the new operation.

In this design, `executed_sql` means the target-dialect SQL Wren hands to the
connector. A connector may apply its normal driver-specific row-limit wrapper;
the audit record therefore also stores the explicit `row_limit`. Capturing the
driver's private final string across every connector is outside this scope.

## 8. SQL Attempts and Audit Schema

LangGraph checkpoint tables are owned by the checkpointer package. Application
audit data uses two separate tables.

### 8.1 `chat_audit_requests`

One row represents one call to `POST /v1/chat`.

| Column | Type | Constraint and meaning |
|---|---|---|
| `request_id` | UUID | Primary key, generated by the service |
| `session_id` | TEXT | Required, original external session ID |
| `thread_id` | TEXT | Required, derived LangGraph thread ID |
| `question` | TEXT | Required, original user question |
| `answer` | TEXT | Nullable until a final answer exists |
| `status` | TEXT | `running`, `succeeded`, or `failed` |
| `error` | JSONB | Nullable structured whole-request error |
| `started_at` | TIMESTAMPTZ | Required |
| `completed_at` | TIMESTAMPTZ | Nullable until terminal |

Indexes cover `(session_id, started_at DESC)`, `thread_id`, and
`(status, started_at)`.

### 8.2 `chat_sql_attempts`

One row represents one `audited_wren_query` call.

| Column | Type | Constraint and meaning |
|---|---|---|
| `attempt_id` | UUID | Primary key |
| `request_id` | UUID | Required foreign key to `chat_audit_requests` |
| `sequence` | INTEGER | Required, starts at 1, unique within request |
| `semantic_sql` | TEXT | Required, SQL supplied by the model |
| `executed_sql` | TEXT | Nullable when planning did not finish |
| `status` | TEXT | `running`, `success`, or `failed` |
| `row_limit` | INTEGER | Maximum number of rows retained |
| `returned_row_count` | INTEGER | Number of result rows actually persisted |
| `result_truncated` | BOOLEAN | Whether at least one additional row existed |
| `result` | JSONB | Nullable; success columns and retained rows |
| `error` | JSONB | Nullable structured attempt error |
| `started_at` | TIMESTAMPTZ | Required |
| `completed_at` | TIMESTAMPTZ | Nullable while running; required when terminal |
| `duration_ms` | INTEGER | Nullable while running; otherwise non-negative total plan and query duration |

Constraints enforce:

```text
UNIQUE(request_id, sequence)
status = running -> result IS NULL AND error IS NULL
status = success -> result IS NOT NULL AND error IS NULL
status = failed  -> result IS NULL AND error IS NOT NULL
status = running -> completed_at IS NULL AND duration_ms IS NULL
status IN (success, failed) -> completed_at IS NOT NULL AND duration_ms IS NOT NULL
returned_row_count >= 0
row_limit >= 1
```

A newly inserted running attempt has `executed_sql=null`,
`returned_row_count=0`, `result_truncated=false`, `result=null`, and
`error=null`. Planning success updates `executed_sql` before database
execution without changing the status. All terminal updates use
`WHERE status='running'` so a recovery task and a late worker cannot both
finalize the same attempt.

Attempts are read in ascending `sequence` order. Timestamp order is not used
to reconstruct the retry chain.

### 8.3 Success attempt example

```json
{
  "sequence": 2,
  "semantic_sql": "SELECT SUM(amount) AS total_sales FROM orders",
  "executed_sql": "WITH orders AS (...) SELECT SUM(amount) AS total_sales FROM orders",
  "status": "success",
  "row_limit": 100,
  "returned_row_count": 1,
  "result_truncated": false,
  "result": {
    "columns": ["total_sales"],
    "rows": [{"total_sales": "1280000.00"}]
  },
  "error": null
}
```

### 8.4 Failed attempt example

```json
{
  "sequence": 1,
  "semantic_sql": "SELECT SUM(sales_amount) FROM orders",
  "executed_sql": null,
  "status": "failed",
  "row_limit": 100,
  "returned_row_count": 0,
  "result_truncated": false,
  "result": null,
  "error": {
    "code": "INVALID_SQL",
    "phase": "SQL_PLANNING",
    "message": "Column sales_amount does not exist",
    "metadata": {}
  }
}
```

### 8.5 Result normalization

- Dates and times become ISO 8601 strings.
- `Decimal` values become strings to preserve precision.
- `NaN` and positive or negative infinity become `null`.
- Byte values are not stored verbatim. A bounded safe representation is used,
  or the attempt fails with `RESULT_SERIALIZATION_FAILED`.
- Secret-like metadata keys containing password, secret, token, or credential
  are recursively redacted before persistence.

### 8.6 Canonical internal audit representation

Internal audit readers join one `chat_audit_requests` row with its
`chat_sql_attempts` rows ordered by ascending `sequence`. The canonical JSON
representation is:

```json
{
  "session_id": "5d3b168f-5dc3-4c6b-9239-bbb0edbb25c7",
  "question": "上个月销售额是多少？",
  "answer": "上个月销售额为 128 万元。",
  "sql_attempts": [
    {
      "sequence": 1,
      "semantic_sql": "SELECT SUM(sales_amount) FROM orders",
      "executed_sql": null,
      "status": "failed",
      "row_limit": 100,
      "returned_row_count": 0,
      "result_truncated": false,
      "result": null,
      "error": {
        "code": "INVALID_SQL",
        "phase": "SQL_PLANNING",
        "message": "Column sales_amount does not exist",
        "metadata": {}
      }
    },
    {
      "sequence": 2,
      "semantic_sql": "SELECT SUM(amount) AS total_sales FROM orders",
      "executed_sql": "WITH orders AS (...) SELECT SUM(amount) AS total_sales FROM orders",
      "status": "success",
      "row_limit": 100,
      "returned_row_count": 1,
      "result_truncated": false,
      "result": {
        "columns": ["total_sales"],
        "rows": [{"total_sales": "1280000.00"}]
      },
      "error": null
    }
  ]
}
```

This representation is internal only. It is not the response of
`POST /v1/chat`. The main request status, timestamps, durations, and internal
IDs remain available in the underlying audit records even though they are not
repeated in this business-facing audit view.

While a request is live, this view may contain a `running` attempt with no
result or error. After interruption recovery, that entry becomes a failed
attempt with `error.code=SQL_ATTEMPT_INTERRUPTED`; completed audit views do not
leave stale attempts in `running` indefinitely.

## 9. Result Limits and Completeness

The default `row_limit` is 100 and the hard maximum is 1,000. The caller
cannot override these values through the HTTP request.

For a requested limit `N`, the connector is asked for `N+1` rows:

- zero through `N` rows returned: persist all rows and set
  `result_truncated=false`;
- `N+1` rows returned: persist the first `N`, set
  `returned_row_count=N`, and set `result_truncated=true`.

These limits apply only to the final result set transferred from the database.
Database-side aggregation still evaluates all qualifying rows. For example,
`SELECT SUM(amount) ...` computes across the complete filtered dataset and
usually returns one row.

The agent prompt must require aggregation, filtering, or grouping for large
datasets. It must not claim complete detail coverage when
`result_truncated=true`.

The retained rows are logged in full up to `row_limit`; there is no additional
sampling layer. If even one row is too large for the configured serialized
result-size ceiling, the attempt fails with `RESULT_TOO_LARGE`, allowing the
model to select fewer columns or aggregate and retry.

## 10. Persistence and Transaction Semantics

Audit evidence is written incrementally rather than at the end of the LLM
run:

1. insert a `chat_audit_requests` row with `status=running`;
2. insert a `chat_sql_attempts` row with `status=running` before planning;
3. plan the SQL and persist `executed_sql` before business-database execution;
4. execute the SQL;
5. atomically update the attempt to `success` with its result or `failed` with
   its structured error;
6. repeat steps 2 through 5 for corrections;
7. persist the final compact LangGraph conversation state;
8. update the request row with `answer`, `status=succeeded`, and
   `completed_at`;
9. return the public success response.

No database transaction remains open during an LLM or business-database call.
The initial attempt insert closes the audit gap where SQL could reach the
database and then disappear from the log after a process crash. Persisting
`executed_sql` before database execution further distinguishes an interrupted
planning attempt (`executed_sql=null`) from an attempt that may already have
reached the database (`executed_sql` is populated).

The service returns success only after both conversation state and final audit
status have been persisted. If required audit persistence fails, the API must
not silently return a successful answer.

A recovery job considers an attempt interrupted only when both its session
lease has expired and its `started_at` is older than the configured
interruption threshold (default 150 seconds). It first atomically changes each
qualifying still-running SQL attempt to `failed` with:

```json
{
  "code": "SQL_ATTEMPT_INTERRUPTED",
  "phase": "SQL_EXECUTION",
  "message": "The service stopped before the SQL attempt outcome was recorded.",
  "metadata": {"outcome": "unknown"}
}
```

It sets `completed_at` to the recovery time, calculates `duration_ms`, retains
the existing `semantic_sql` and `executed_sql`, and leaves result fields empty.
The recovery phase is `SQL_PLANNING` when `executed_sql` is null and
`SQL_EXECUTION` when it is populated; the example above shows the latter.
The `outcome=unknown` metadata is important: a read-only database query may
have completed even though the service crashed before observing or persisting
its result.

After recovering attempts, the job changes the stale main request to `failed`
with `REQUEST_INTERRUPTED`. It does not alter already-terminal attempts. Both
updates are conditional on the row still being `running`, making the recovery
job safe to retry.

## 11. Same-Session Concurrency

Only one active request is allowed per `session_id`; otherwise parallel turns
could read the same prior state and write messages out of order. Different
sessions can run concurrently.

The `chat_session_leases` table contains:

| Column | Meaning |
|---|---|
| `session_id` | Primary key |
| `lease_id` | Random token held by one request |
| `expires_at` | Lease expiry |
| `updated_at` | Last renewal time |

Lease acquisition is atomic. A request that encounters a valid lease returns
`409`. The holder renews its lease while running and releases it on terminal
completion. A crashed holder cannot permanently block the session because the
lease expires.

This design avoids holding a PostgreSQL connection or transaction for the
duration of the LLM operation.

## 12. Multi-Turn Context Management

The durable conversation state retains:

- the user's original questions;
- final natural-language answers;
- a rolling summary of older turns;
- business entities, filters, time ranges, and comparison subjects needed to
  resolve later references.

SQL retry details and full results belong only to the audit tables after a
turn completes. They are removed from durable message state using explicit
LangGraph message-state updates.

The first release retains the most recent six complete question-answer turns.
Older turns are compressed into a rolling summary. A configured token ceiling
provides a second bound. Compaction must preserve facts needed for references
such as “去年呢” and “和刚才的结果比较”.

The checkpoint itself remains authoritative for conversation continuation;
audit records are never replayed to reconstruct model context.

## 13. Execution and Safety Limits

Default limits:

| Limit | Default |
|---|---:|
| Question length | 4,000 characters |
| SQL attempts per request | 3 |
| Default result row limit | 100 |
| Hard result row limit | 1,000 |
| Agent graph steps | 12 |
| Total request time | 120 seconds |
| Concurrent requests per session | 1 |
| Complete recent turns | 6 |
| Dedicated synchronous Wren workers | 16 |

Values are service configuration, not caller-controlled fields. Hard caps
cannot be raised by a model tool argument.

Current Wren and connector tools are synchronous. FastAPI and LangGraph run
asynchronously, while Wren calls use a dedicated bounded worker pool. Queue
admission is also bounded so overload fails predictably instead of creating an
unbounded backlog.

## 14. SQL and Data Security

- Only a single read-only query statement is accepted.
- DML, DDL, transaction statements, and multiple statements are rejected
  before database execution.
- Wren strict mode restricts queries to configured MDL models and views.
- The business database connection must use a read-only database account.
- Query results are untrusted content; values that resemble instructions
  cannot override the system prompt or tool policy.
- `wren_store_query` is disabled for the service agent.
- Public errors are generic and do not expose SQL or driver diagnostics.
- Audit result access is internal and protected by service-level controls.
- Audit retention and deletion periods are deployment configuration because
  result rows may contain business-sensitive data.
- LangGraph and LangChain dependencies must have security version floors
  covering published serializer and checkpoint fixes.

## 15. Failure Handling

An SQL planning or execution failure is returned to the model as a structured
tool error and immediately audited. The model may correct and retry until it
reaches the three-attempt limit.

When the limit is reached, further SQL execution is blocked and the whole
request fails with `SQL_RETRY_EXHAUSTED`. Other whole-request failures include:

- LLM timeout or unavailability;
- agent graph step exhaustion;
- total request timeout;
- Wren or database unavailability;
- checkpoint persistence failure;
- audit persistence failure;
- session lease loss;
- empty or invalid final model output.

The main audit row stores the detailed structured error and becomes `failed`.
The public API returns the stable error envelope defined in Section 5.3.

## 16. Observability

Structured application logs contain the internal `request_id`, a hashed
session identifier, status, duration, model-call count, SQL-attempt count, and
error code. They do not duplicate full result rows or secrets from the audit
database.

Required metrics:

- request count, success rate, and latency;
- active session leases and `409` conflicts;
- LLM latency and failures;
- SQL attempt count, planning failures, execution failures, and retries;
- result truncation count;
- Wren worker-pool saturation;
- checkpoint and audit persistence failures;
- stale `running` requests recovered.

## 17. Verification and Acceptance Criteria

Automated tests must demonstrate:

1. A later request with the same `session_id` can refer to earlier context.
2. Different session IDs never share conversation state.
3. Concurrent calls for one session return `409` for the loser.
4. Different sessions can execute concurrently.
5. Conversation state survives a service restart.
6. A planning failure stores `executed_sql=null`.
7. An execution failure stores both semantic and executed SQL.
8. A failed query followed by a corrected query produces sequences 1 and 2.
9. Multiple successful SQL calls retain their real execution order.
10. Every SQL tool invocation inserts a `running` attempt before Wren planning
    or connector execution begins.
11. Planned target SQL is committed to the running attempt before the
    connector receives it.
12. An `N+1` result sets `returned_row_count=N` and
    `result_truncated=true`.
13. Database aggregation uses all qualifying rows despite the result limit.
14. Audit persistence failure prevents a public success response.
15. A process interruption leaves a running attempt that recovery converts to
    `failed` with `SQL_ATTEMPT_INTERRUPTED` and an unknown outcome.
16. Recovery distinguishes an interrupted planning attempt from an interrupted
    database-execution attempt using the presence of `executed_sql`.
17. An expired session lease can be acquired by a later request.
18. DML, DDL, transaction, and multi-statement SQL never reach the connector.
19. A fourth SQL attempt is rejected after three attempts.
20. Compacted context still resolves references to older filters and periods.
21. A success response contains exactly `session_id` and `answer`.
22. Dates, decimals, non-finite floats, and byte values follow normalization
    rules.
23. Error metadata and application logs do not leak credentials.
24. A clarification answer succeeds with zero SQL attempts.
25. A large single row produces a structured serialization or size error and
    can be followed by a corrected query.

Unit tests use deterministic fake models and fake Wren tools. PostgreSQL
integration tests cover checkpoint recovery, audit constraints, short
transactions, and lease contention. At least one end-to-end test uses a local
DuckDB Wren project to prove that the tool executes real Wren-planned SQL and
captures both query forms.

## 18. Delivery Phases

### Phase 1: Wren execution evidence

Add the backward-compatible Wren SDK operation that returns planned SQL with
the query result. Prove read-only validation and `N+1` semantics with direct
tests.

### Phase 2: Service foundation and persistence

Create the FastAPI service, configuration, PostgreSQL migrations, audit
repositories, checkpoint initialization, and session lease component.

### Phase 3: Audited agent

Build the LangGraph agent, audited SQL tool, incremental attempt persistence,
pre-execution `running` records, interrupted-attempt recovery, retry limits,
result normalization, and compact conversation state.

### Phase 4: HTTP behavior and failure mapping

Implement the exact request/response schemas, authentication, concurrency
behavior, timeouts, and stable public error mapping.

### Phase 5: Verification and operations

Add isolation, restart, concurrency, audit, end-to-end, security, and load
tests. Add health checks, metrics, container packaging, and deployment
documentation.

## 19. Final Decisions

- The API answers Wren-backed business-data questions, not generic chat.
- The caller provides only `session_id` and `question`; there is no tenant or
  user identity dimension.
- The success response contains only `session_id` and `answer`.
- All SQL attempts and retained results are internal audit data.
- Failed and corrected SQL attempts are preserved in real execution order.
- Result limits constrain transferred rows, not database-side aggregation.
- Conversation checkpoints and audit records are independent PostgreSQL data.
- The service is a new deployable module and does not turn `core/wren` into an
  LLM server.
