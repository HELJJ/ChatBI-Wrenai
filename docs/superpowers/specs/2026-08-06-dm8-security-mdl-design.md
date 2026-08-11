# DM8 Security MDL Package Design

## Goal

Generate a Wren v5 MDL package for nine confirmed tables in the
`SAFETY_RISK_NEW` DM8 schema, suitable for governed natural-language queries
through Wren MCP.

## Models

Expose business-facing model names while preserving exact physical table and
column identifiers:

- `security_architecture` -> `sec_security_architecture`
- `security_database` -> `sec_security_database`
- `security_data_exchange` -> `sec_security_dataexchange`
- `security_middleware` -> `sec_security_middleware`
- `security_os` -> `sec_security_os`
- `security_plugin` -> `sec_security_plugin`
- `security_port` -> `sec_security_port`
- `security_resource` -> `sec_security_resource`
- `security_system` -> `SEC_SYSTEM_INFO`

All 204 columns are sourced from `columns.csv`. `F_ID` is the primary key of
every model. Column descriptions come from `comments.csv`; missing comments
are stated as unavailable rather than inferred.

## Type normalization

- `VARCHAR` and `VARCHAR2` become `VARCHAR(length)` when a length is present.
- `INT`, `BIGINT`, `TEXT`, `TIMESTAMP`, `DATETIME`, and `DATE` keep their Wren
  canonical spelling.

## Relationships

Create ten user-approved logical `MANY_TO_ONE` relationships. They are
business-semantic relationships rather than DM8-declared foreign keys:

1. architecture -> system
2. database -> system
3. data exchange -> system
4. middleware -> system
5. middleware -> resource
6. operating system -> system
7. operating system -> resource
8. plugin -> system
9. port -> system
10. resource -> system

Joins use identifier columns, never redundant name columns.

## Knowledge rules

The package documents that queries should use only these nine models by
default, exclude rows where `F_DELETEMARK != 0`, apply `F_ENABLEDMARK = 1`
when the user asks for current or enabled assets, prefer ID joins, remain
read-only, and limit exploratory results to 100 rows.

## Verification

The generated project must contain nine model files, 204 columns, nine primary
keys, and ten relationships. `wren context validate` and `wren context build`
must succeed locally. Live DM8 verification remains a server-side smoke test
using `dry-plan`, `dry-run`, and one limited query per model.
