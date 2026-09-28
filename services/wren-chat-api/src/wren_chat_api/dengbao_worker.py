"""Background worker for dengbao report extraction tasks.

One worker, one task at a time — the docling parsing service behind it is
single-concurrency too, so parallel claims would only queue there. The
loop mirrors the recovery loop's shape (stop event + shielded task), with
one deliberate difference: shutdown CANCELS an in-flight conversion after
signalling (a 14-minute conversion must not block service shutdown). The
cancelled row stays 'running' and startup's reset_stale_running returns
it to pending, so the work is re-run, never lost.

Result shape stored in the task row and served by GET /v1/report-tasks:

    {"reportType": "dengbao", "detail": <报告提取结果 15 个中文顶层键>}
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from wren_chat_api.config import Settings
from wren_chat_api.errors import ChatServiceError, UpstreamFailed
from wren_chat_api.metrics import REPORT_TASK_PENDING, REPORT_TASKS
from wren_chat_api.report_tasks import ReportTaskRepository

logger = logging.getLogger(__name__)

_FAILED_MESSAGE = "报告解析失败，请确认文件完整后重试。"


async def run_report_task_loop(
    *,
    repo: ReportTaskRepository,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    """Claim and process dengbao tasks until stopped."""
    recovered = await repo.reset_stale_running()
    if recovered:
        logger.info("report task recovery: %d running rows reset to pending",
                    recovered)
    async with httpx.AsyncClient(
        base_url=settings.docling_service_url,
        timeout=httpx.Timeout(settings.dengbao_docling_timeout_seconds),
    ) as client:
        while not stop_event.is_set():
            try:
                REPORT_TASK_PENDING.set(await repo.count_pending())
                task = await repo.claim_next_pending()
                if task is None:
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=settings.report_task_poll_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                await _process_one(task, repo=repo, client=client)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("report task loop iteration failed", exc_info=True)
                await asyncio.sleep(1)


async def _process_one(
    task: dict[str, Any],
    *,
    repo: ReportTaskRepository,
    client: httpx.AsyncClient,
) -> None:
    """Run one claimed task to a terminal state; never raises."""
    started = time.monotonic()
    try:
        result = await _execute(task, client)
        await repo.succeed(task["task_id"], result=result)
        REPORT_TASKS.labels(outcome="succeeded").inc()
        logger.info(
            "report task %s done for %s in %.1fs",
            task["task_id"], task["filename"], time.monotonic() - started,
        )
    except ChatServiceError as exc:
        await repo.fail(
            task["task_id"], code=exc.code, message=exc.public_message
        )
        REPORT_TASKS.labels(outcome="failed").inc()
        logger.warning(
            "report task %s failed: %s",
            task["task_id"], exc.internal_message or exc.code,
        )
    except Exception:
        await repo.fail(
            task["task_id"], code="REPORT_TASK_FAILED", message=_FAILED_MESSAGE
        )
        REPORT_TASKS.labels(outcome="failed").inc()
        logger.error("report task %s crashed", task["task_id"], exc_info=True)


async def _execute(task: dict[str, Any], client: httpx.AsyncClient) -> dict:
    """Convert one task's bytes via the parsing service, then post-process."""
    response = await client.post(
        "/convert",
        params={"preset": "standard", "formats": "json"},
        files={
            "file": (
                task["filename"],
                bytes(task["file_bytes"]),
                "application/pdf",
            )
        },
    )
    if response.status_code >= 500:
        raise UpstreamFailed(
            f"docling service {response.status_code}: "
            f"{response.text[:200]}"
        )
    response.raise_for_status()
    docling_json = response.json()["docling_json"]

    from report_postproc import dengbao_extract

    # The pipeline needs the PDF on disk (printed-page mapping + VLM-task
    # page rendering); the stem only feeds output naming here.
    stem = Path(task["filename"]).stem or "document"
    safe_stem = re.sub(r"[/\\\x00-\x1f]+", "_", stem)[:150] or "document"
    tmp_dir = tempfile.mkdtemp(prefix="dengbao_task_")
    try:
        pdf_path = os.path.join(tmp_dir, safe_stem + ".pdf")
        with open(pdf_path, "wb") as handle:
            handle.write(bytes(task["file_bytes"]))
        result = await asyncio.to_thread(dengbao_extract, pdf_path, docling_json)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    report = result["report"]
    return {"reportType": "dengbao", "detail": report}


def task_view(task: dict[str, Any]) -> dict[str, Any]:
    """Serialize one repository row for GET /v1/report-tasks/{taskId}."""
    view: dict[str, Any] = {
        "taskId": str(task["task_id"]),
        "filename": task["filename"],
        "reportType": task["doc_type"],
        "status": (
            "processing"
            if task["status"] in ("pending", "running")
            else task["status"]
        ),
        "createdAt": task["created_at"].isoformat(),
        "completedAt": (
            task["completed_at"].isoformat()
            if task["completed_at"] is not None
            else None
        ),
        "result": task["result"],
    }
    if task["status"] == "failed":
        view["error"] = {
            "code": task["error_code"] or "REPORT_TASK_FAILED",
            "message": task["error_message"] or _FAILED_MESSAGE,
        }
    else:
        view["error"] = None
    return view


def parse_task_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None
