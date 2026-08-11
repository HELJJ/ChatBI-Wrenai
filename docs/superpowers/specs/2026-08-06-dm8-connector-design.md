# DM8 Connector Design

## Goal

Add read-only DM8 query support to the `wrenai` Python SDK and CLI using the
official `dmPython` DB-API driver. Users must be able to create a `dm8`
profile, build a Wren context project, plan semantic SQL, execute it against
DM8, and expose the same operations through the existing MCP server.

## Observed behavior

The target DM8 instance is reachable with `dmPython` 2.5.38 on Python 3.11.
Its `COMPATIBLE_MODE` is `4` (MySQL compatibility), but behavioral probes show:

- `LIMIT`, `IFNULL`, and `DATE_FORMAT` execute successfully.
- MySQL backtick-quoted identifiers fail with DM error `-2007`.
- Oracle-style `ROWNUM`, `NVL`, `FETCH FIRST`, `DUAL`, and CTE syntax execute
  successfully.

Because SQLGlot's MySQL dialect emits backticks for quoted identifiers, DM8
must not use the MySQL planning dialect. The initial connector will use Oracle
as its planning and rendering dialect while executing through `dmPython`.

## Architecture

DM8 is a first-class Python-facing `DataSource`, but an alias of Oracle at the
Rust planning boundary. This keeps the native driver and profile semantics
distinct without requiring a new Rust engine release.

The data flow is:

1. A project or profile selects `dm8`.
2. Python validates connection settings with `DM8ConnectionInfo`.
3. Wren SQL planning maps `dm8` to `oracle` in both the manifest and data-source
   argument before either reaches wren-core, and maps it to Oracle in SQLGlot.
4. `DM8Connector` executes the resulting SQL using `dmPython`.
5. Query rows are converted to a PyArrow table and returned through the
   existing SDK, CLI, or MCP path.

## Components

### Data source and connection model

Add `DataSource.dm8` and a `DM8ConnectionInfo` model with:

- `host` (required)
- `port` (default `5236`)
- `user` (required)
- `password` (optional `SecretStr`)
- `schema` (optional; when present, selected after connection)

Register this model in the shared field registry so interactive profiles, the
browser form, generated connection documentation, and secret masking all use
the same definition.

### Dialect alias

Map `DataSource.dm8` to SQLGlot's `oracle` dialect. Before creating the
wren-core `SessionContext` or `ManifestExtractor`, map both the explicit
data-source name and a top-level manifest `dataSource: dm8` to the
Rust-supported `oracle` value. Keep this alias in explicit helper functions
rather than scattering conditionals across the engine.

Models and views may continue to use `dialect: oracle`; `dm8` is a connection
target, not a new Rust manifest dialect in this iteration.

### Native connector

Add `wren.connector.dm8` with the standard connector interface:

- Connect using `dmPython.connect(server=..., port=..., user=..., password=...)`.
- When `schema` is configured, validate it as a simple SQL identifier and run
  `SET SCHEMA "<schema>"`. It must never accept an arbitrary SQL fragment.
- Strip a single trailing statement semicolon.
- Apply result limits using `SELECT * FROM (<sql>) t WHERE ROWNUM <= <limit>`.
- Implement dry-run using the same wrapper with `ROWNUM <= 0`.
- Convert DB-API results to PyArrow, preserving nulls and common Python scalar
  types; explicitly normalize driver LOB values when required.
- Convert driver database errors to `WrenError` with the SQL execution phase
  and dialect SQL metadata.
- Close cursors deterministically and make connector close idempotent.

Only read/query behavior is in scope. No DM8-specific write operations,
schema mutation, or introspection generator is added.

### Packaging

Add a `dm8` optional dependency containing `dmPython>=2.5` and include it in
the `all` extra. Factory import errors must tell users to install
`wrenai[dm8]`.

## User configuration

A project selects DM8 with:

```yaml
data_source: dm8
```

The profile stores non-secret connection fields and references the password
through environment expansion:

```yaml
datasource: dm8
host: dm.example.internal
port: "5236"
user: APP
password: ${DM_PASSWORD}
schema: APP
```

Credentials must not be committed to the repository or embedded into tests.

## Error handling

- Missing `dmPython`: actionable `pip install 'wrenai[dm8]'` error.
- Invalid schema identifier: connection-info error before SQL execution.
- Authentication/network failure: wrapped user-facing connection/query error
  without logging the password.
- DM8 SQL failure: preserve the driver message and attach the generated SQL in
  structured error metadata.
- Arrow conversion failure: report the affected column/type without exposing
  connection secrets.

## Testing

Unit tests will use a mocked `dmPython` module and exercise behavior rather
than source text:

- connection arguments and optional schema selection
- invalid schema rejection
- query execution and Arrow conversion
- limit and dry-run wrappers
- driver error translation
- idempotent close
- data-source parsing, field registry, factory routing, and install hint
- DM8-to-Oracle planning aliases

The real DM8 instance is not a CI dependency. A documented manual smoke test
will cover connection, CTE, aggregation, join, date expression, dry-run, and a
limited query against a user-owned schema.

## Out of scope and evolution

A dedicated Rust `DataSource::DM8` and full DM8 SQL dialect are deferred. If
real workload tests expose Oracle-alias incompatibilities, add focused
DM8-specific rewrites backed by reproducible queries. A full Rust dialect is
only justified when those differences cannot be contained in the Python
dialect/connector boundary.
