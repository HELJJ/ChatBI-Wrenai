"""Pentest second extraction channel: docling service + rule-based repair.

Runs AFTER the primary VLM pipeline inside the pentest route. The upload
bytes go to the local docling parsing service (preset=pentest_fix), and
the returned docling JSON is repaired and structurally extracted by
``report_postproc.pentest_detail`` (cross-page table stitching, heading
repair, structured field extraction — the 阶段二 对拍-validated chain).

The channel is additive and fail-open: ``extract_detail`` never raises.
Any failure — service down, conversion error, post-processing error — is
logged and degrades to ``detail=None`` so the primary three fields are
never affected.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx

from wren_chat_api.config import Settings
from wren_chat_api.metrics import PENTEST_DOCLING

logger = logging.getLogger(__name__)


class PentestDoclingChannel:
    """Additive docling channel behind POST /v1/pentest-report/extract."""

    def __init__(
        self,
        *,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.docling_service_url,
            timeout=httpx.Timeout(settings.pentest_docling_timeout_seconds),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def extract_detail(
        self, filename: str, raw: bytes
    ) -> dict[str, Any] | None:
        """Best-effort detail extraction; never raises, None on failure."""
        try:
            detail = await self._extract(filename, raw)
        except Exception:
            PENTEST_DOCLING.labels(outcome="degraded").inc()
            logger.warning(
                "pentest docling channel degraded for %s",
                filename,
                exc_info=True,
            )
            return None
        PENTEST_DOCLING.labels(outcome="ok").inc()
        return detail

    async def _extract(self, filename: str, raw: bytes) -> dict[str, Any]:
        from report_postproc import pentest_detail

        response = await self._client.post(
            "/convert",
            params={"preset": "pentest_fix", "formats": "json"},
            files={"file": (filename, raw, "application/pdf")},
        )
        response.raise_for_status()
        docling_json = response.json()["docling_json"]

        # The post-processing needs the PDF on disk for bookmark-based
        # heading repair, and the file stem feeds the 文件名称 field —
        # keep the original filename, sanitised only for path safety.
        stem = Path(filename).stem or "document"
        safe_stem = re.sub(r"[/\\\x00-\x1f]+", "_", stem)[:150] or "document"
        tmp_dir = tempfile.mkdtemp(prefix="pentest_docling_")
        try:
            pdf_path = os.path.join(tmp_dir, safe_stem + ".pdf")
            with open(pdf_path, "wb") as handle:
                handle.write(raw)
            result = await asyncio.to_thread(pentest_detail, pdf_path, docling_json)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        detail = result["detail"]
        logger.info(
            "pentest docling detail for %s: 风险项 %d / 通过项 %d / 不适用 %d "
            "in %.1fs",
            filename,
            len(detail.get("安全风险项") or []),
            len(detail.get("测试通过项") or []),
            len(detail.get("测试不适用项") or []),
            result["duration_seconds"],
        )
        return detail
