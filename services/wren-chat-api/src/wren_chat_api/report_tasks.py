"""Dengbao (等级保护测评) PDF intake: type detection and task repository.

The risk-assessment endpoint accepts dengbao PDFs as an additive branch:
a cheap text scan classifies the upload, then a task row is created (or a
matching one reused — identical bytes never re-run the 14-minute docling
conversion) and a background worker processes it. See dengbao_worker.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from psycopg_pool import AsyncConnectionPool

# Cover-page / basic-info phrasing shared by this report family. The scan
# reads only the first pages, so keywords must occur there in practice
# (they do: title page, 基本信息表, 结论表 all sit up front).
_DENGBAO_KEYWORDS = ("等级保护测评", "网络安全等级测评", "等级测评结论")


def looks_like_dengbao(raw: bytes, *, max_pages: int = 8) -> bool:
    """Whether the first pages' text layer marks the PDF as a dengbao report.

    Runs pypdfium2 synchronously (a handful of pages only); callers in
    async contexts wrap it in a thread.
    """
    import io

    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(io.BytesIO(raw))
    except Exception:
        return False
    try:
        text = ""
        for index in range(min(len(pdf), max_pages)):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                text += textpage.get_text_bounded() or ""
            finally:
                textpage.close()
        compact = "".join(text.split())
        return any(keyword in compact for keyword in _DENGBAO_KEYWORDS)
    finally:
        pdf.close()


class ReportTaskRepository:
    """CRUD + claim semantics for report_tasks rows."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def create_or_reuse(
        self, *, filename: str, raw: bytes
    ) -> dict[str, Any]:
        """Insert a pending dengbao task, or reuse a matching row.

        Reuse rules (same sha256): an in-flight task (pending/running) is
        returned as-is — concurrent uploads of one document share one
        conversion; a succeeded task is returned so the cached result is
        served without re-running; failed tasks are NOT reused — a fresh
        upload retries. The advisory lock serializes same-hash races.
        """
        sha = hashlib.sha256(raw).hexdigest()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))", (sha,)
                )
                in_flight = await conn.execute(
                    """
                    SELECT * FROM report_tasks
                    WHERE file_sha256 = %s
                      AND status IN ('pending', 'running')
                    ORDER BY created_at
                    LIMIT 1
                    """,
                    (sha,),
                )
                row = await in_flight.fetchone()
                if row is not None:
                    row["reused"] = "in_flight"
                    return row
                done = await conn.execute(
                    """
                    SELECT * FROM report_tasks
                    WHERE file_sha256 = %s AND status = 'succeeded'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (sha,),
                )
                row = await done.fetchone()
                if row is not None:
                    row["reused"] = "cache"
                    return row
                inserted = await conn.execute(
                    """
                    INSERT INTO report_tasks
                        (task_id, filename, file_sha256, file_bytes,
                         doc_type, status)
                    VALUES (%s, %s, %s, %s, 'dengbao', 'pending')
                    RETURNING *
                    """,
                    (uuid.uuid4(), filename, sha, raw),
                )
                row = await inserted.fetchone()
                row["reused"] = "created"
                return row

    async def get(self, task_id: uuid.UUID) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM report_tasks WHERE task_id = %s", (task_id,)
            )
            return await cursor.fetchone()

    async def claim_next_pending(self) -> dict[str, Any] | None:
        """Atomically take the oldest pending task (single worker)."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    """
                    UPDATE report_tasks
                    SET status = 'running', started_at = now()
                    WHERE task_id = (
                        SELECT task_id FROM report_tasks
                        WHERE status = 'pending'
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING *
                    """
                )
                return await cursor.fetchone()

    async def succeed(
        self, task_id: uuid.UUID, *, result: dict[str, Any]
    ) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE report_tasks
                SET status = 'succeeded', result = %s::jsonb,
                    completed_at = now()
                WHERE task_id = %s
                """,
                (json.dumps(result, ensure_ascii=False), task_id),            )
            if cursor.rowcount != 1:
                raise LookupError(f"task {task_id} vanished before success")

    async def fail(
        self, task_id: uuid.UUID, *, code: str, message: str
    ) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE report_tasks
                SET status = 'failed', error_code = %s, error_message = %s,
                    completed_at = now()
                WHERE task_id = %s
                """,
                (code, message, task_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"task {task_id} vanished before failure")

    async def count_pending(self) -> int:
        """Queue depth for the backlog gauge (cheap count each poll)."""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) AS n FROM report_tasks"
                " WHERE status = 'pending'"
            )
            row = await cursor.fetchone()
            return int(row["n"])

    async def reset_stale_running(self) -> int:
        """Return interrupted running rows to pending (startup recovery)."""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE report_tasks
                SET status = 'pending', started_at = NULL
                WHERE status = 'running'
                """
            )
            return cursor.rowcount
