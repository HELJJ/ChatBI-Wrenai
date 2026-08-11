from __future__ import annotations

import re
from typing import Any

import dmPython
import pyarrow as pa

from wren.connector.base import ConnectorABC, coerce_limit, strip_trailing_semicolon
from wren.model.error import DIALECT_SQL, ErrorCode, ErrorPhase, WrenError

_SCHEMA_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*\Z")


def _validate_schema(schema: str | None) -> str | None:
    if schema is None:
        return None
    if not _SCHEMA_RE.fullmatch(schema):
        raise WrenError(
            ErrorCode.INVALID_CONNECTION_INFO,
            "DM8 schema must be a simple SQL identifier",
            phase=ErrorPhase.VALIDATION,
        )
    return schema


def _normalize_value(value: Any) -> Any:
    if isinstance(value, memoryview):
        return bytes(value)
    reader = getattr(value, "read", None)
    if callable(reader):
        return reader()
    return value


def _build_arrow_table(cursor) -> pa.Table:
    if cursor.description is None:
        return pa.table({})

    rows = cursor.fetchall()
    names = [description[0] for description in cursor.description]
    columns = [[] for _ in names]
    for row in rows:
        for index, value in enumerate(row):
            columns[index].append(_normalize_value(value))

    arrays = [pa.array(values) for values in columns]
    return pa.Table.from_arrays(arrays, names=names)


class DM8Connector(ConnectorABC):
    def __init__(self, connection_info) -> None:
        schema = _validate_schema(connection_info.dm_schema)
        password = (
            connection_info.password.get_secret_value()
            if connection_info.password is not None
            else None
        )
        try:
            self.connection = dmPython.connect(
                server=connection_info.host,
                port=int(connection_info.port),
                user=connection_info.user,
                password=password,
            )
            if schema is not None:
                self._set_schema(schema)
        except dmPython.DatabaseError as exc:
            connection = getattr(self, "connection", None)
            if connection is not None:
                connection.close()
                self.connection = None
            raise WrenError(
                ErrorCode.GET_CONNECTION_ERROR,
                str(exc),
                phase=ErrorPhase.VALIDATION,
            ) from exc

    def _set_schema(self, schema: str) -> None:
        cursor = self.connection.cursor()
        try:
            cursor.execute(f'SET SCHEMA "{schema}"')
        finally:
            cursor.close()

    def query(self, sql: str, limit: int | None = None) -> pa.Table:
        limit = coerce_limit(limit)
        executed_sql = strip_trailing_semicolon(sql)
        if limit is not None:
            executed_sql = f"SELECT * FROM ({executed_sql}) t WHERE ROWNUM <= {limit}"

        cursor = self.connection.cursor()
        try:
            cursor.execute(executed_sql)
            return _build_arrow_table(cursor)
        except dmPython.DatabaseError as exc:
            raise WrenError(
                ErrorCode.INVALID_SQL,
                str(exc),
                phase=ErrorPhase.SQL_EXECUTION,
                metadata={DIALECT_SQL: executed_sql},
            ) from exc
        finally:
            cursor.close()

    def dry_run(self, sql: str) -> None:
        executed_sql = (
            f"SELECT * FROM ({strip_trailing_semicolon(sql)}) t WHERE ROWNUM <= 0"
        )
        cursor = self.connection.cursor()
        try:
            cursor.execute(executed_sql)
        except dmPython.DatabaseError as exc:
            raise WrenError(
                ErrorCode.INVALID_SQL,
                str(exc),
                phase=ErrorPhase.SQL_DRY_RUN,
                metadata={DIALECT_SQL: executed_sql},
            ) from exc
        finally:
            cursor.close()

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None


def create_connector(connection_info) -> DM8Connector:
    return DM8Connector(connection_info)
