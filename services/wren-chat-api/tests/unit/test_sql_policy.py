"""Unit contracts for the read-only SQL policy gate."""

import pytest

from wren_chat_api.errors import ReadOnlySqlRequired
from wren_chat_api.sql_policy import validate_read_only_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM orders",
        "WITH x AS (SELECT * FROM orders) SELECT COUNT(*) FROM x",
        "SELECT region FROM orders UNION ALL SELECT region FROM returns",
    ],
)
def test_read_only_queries_are_allowed(sql: str) -> None:
    validate_read_only_sql(sql, dialect="postgres")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE orders SET status='x'",
        "INSERT INTO orders VALUES (1)",
        "DROP TABLE orders",
        "BEGIN",
        "SELECT 1; SELECT 2",
    ],
)
def test_non_read_only_or_multiple_statements_are_rejected(sql: str) -> None:
    with pytest.raises(ReadOnlySqlRequired):
        validate_read_only_sql(sql, dialect="postgres")


def test_unparsable_sql_is_rejected() -> None:
    with pytest.raises(ReadOnlySqlRequired):
        validate_read_only_sql("!! not sql at all", dialect="postgres")


def test_write_hidden_in_cte_is_rejected() -> None:
    sql = (
        "WITH removed AS (DELETE FROM orders RETURNING *) "
        "SELECT COUNT(*) FROM removed"
    )
    with pytest.raises(ReadOnlySqlRequired):
        validate_read_only_sql(sql, dialect="postgres")
