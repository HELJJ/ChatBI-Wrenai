"""One-statement, read-only SQL validation based on sqlglot ASTs."""

from __future__ import annotations

import sqlglot
from sqlglot import expressions as exp

from wren_chat_api.errors import ReadOnlySqlRequired

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.AlterTable,
    exp.TruncateTable,
    exp.Grant,
    exp.Revoke,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Command,
    exp.Set,
    exp.Use,
    exp.Copy,
    exp.Into,
)


def validate_read_only_sql(sql: str, dialect: str) -> None:
    """Allow exactly one read-only query expression, nothing else.

    Validation walks the parsed AST rather than matching text so that
    comments, casing, dialect spellings, or writes nested inside CTEs
    cannot smuggle a non-read-only statement through.
    """
    if not sql or not sql.strip():
        raise ReadOnlySqlRequired("SQL statement is empty")

    try:
        expressions = sqlglot.parse(sql, dialect=dialect)
    except sqlglot.errors.ParseError as exc:
        raise ReadOnlySqlRequired(
            f"SQL could not be parsed for dialect {dialect!r}",
            cause=exc,
        ) from exc

    expressions = [statement for statement in expressions if statement is not None]
    if len(expressions) != 1:
        raise ReadOnlySqlRequired(
            f"Expected exactly one SQL statement, found {len(expressions)}"
        )

    root = expressions[0]
    if not isinstance(root, exp.Query):
        raise ReadOnlySqlRequired(
            f"Statement is a command, not a query: {type(root).__name__}"
        )

    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise ReadOnlySqlRequired(
                f"Read-only queries cannot contain {type(node).__name__}"
            )
