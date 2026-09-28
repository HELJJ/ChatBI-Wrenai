"""API tests for the dengbao async branch and GET /v1/report-tasks.

The repository is faked in memory (its SQL semantics — dedup, claim,
crash reset — are exercised by the live phase-4 regression); the docling
service is a MockTransport; the pipeline call is monkeypatched. What is
under test here: route branching, envelopes, task-id handling, dedup
semantics visible through the API, and the worker's terminal states.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from wren_chat_api.app import create_app
from wren_chat_api.config import Settings
from wren_chat_api.dengbao_worker import _process_one
from wren_chat_api.report_tasks import looks_like_dengbao
from tests.api.test_pentest_extract_api import (
    _PDF_BYTES,
    client_for,
    make_settings,
)

_PDF_MAGIC_BYTES = b"%PDF-1.7\n%dengbao payload"

_REPORT = {"测评报告名称": "某系统测评报告", "等级测评结论": {"综合得分": 87.69}}


class FakeRepo:
    """In-memory report_tasks with the same reuse semantics as SQL."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def _seed(self, raw: bytes, status: str, result=None) -> dict:
        task_id = str(uuid.uuid4())
        row = {
            "task_id": uuid.UUID(task_id),
            "filename": "报告.pdf",
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "file_bytes": raw,
            "doc_type": "dengbao",
            "status": status,
            "result": result,
            "error_code": None,
            "error_message": None,
            "created_at": datetime.now(timezone.utc),
            "started_at": None,
            "completed_at": None,
        }
        self.rows[task_id] = row
        return row

    async def create_or_reuse(self, *, filename: str, raw: bytes) -> dict:
        sha = hashlib.sha256(raw).hexdigest()
        for row in sorted(
            self.rows.values(), key=lambda r: r["created_at"]
        ):
            if row["file_sha256"] == sha:
                if row["status"] in ("pending", "running"):
                    row["reused"] = "in_flight"
                    return dict(row)
        for row in sorted(
            self.rows.values(), key=lambda r: r["created_at"], reverse=True
        ):
            if row["file_sha256"] == sha and row["status"] == "succeeded":
                row["reused"] = "cache"
                return dict(row)
        row = self._seed(raw, "pending")
        row["filename"] = filename
        row["reused"] = "created"
        return dict(row)

    async def get(self, task_id: uuid.UUID) -> dict | None:
        row = self.rows.get(str(task_id))
        return dict(row) if row else None

    async def claim_next_pending(self) -> dict | None:
        for row in sorted(
            self.rows.values(), key=lambda r: r["created_at"]
        ):
            if row["status"] == "pending":
                row["status"] = "running"
                row["started_at"] = datetime.now(timezone.utc)
                return dict(row)
        return None

    async def succeed(self, task_id, *, result) -> None:
        row = self.rows[str(task_id)]
        row.update(
            status="succeeded", result=result,
            completed_at=datetime.now(timezone.utc),
        )

    async def fail(self, task_id, *, code, message) -> None:
        row = self.rows[str(task_id)]
        row.update(
            status="failed", error_code=code, error_message=message,
            completed_at=datetime.now(timezone.utc),
        )

    async def count_pending(self) -> int:
        return sum(
            1 for row in self.rows.values() if row["status"] == "pending"
        )


def make_app(
    tmp_path,
    repo: FakeRepo | None = None,
    *,
    dengbao: bool = True,
) -> tuple[FastAPI, FakeRepo]:
    repo = repo or FakeRepo()
    app = create_app(
        make_settings(tmp_path, risk_dengbao_enabled=dengbao),
        overrides={
            "risk_assessment_service": _NoopRiskService(),
            "report_task_repo": repo,
        },
    )
    return app, repo


class _NoopRiskService:
    async def extract(self, filename, raw):  # pragma: no cover - unused
        raise AssertionError("doc/docx path must not run in these tests")


def upload_pdf(raw: bytes = _PDF_MAGIC_BYTES, name: str = "报告.pdf") -> dict:
    return {"file": (name, raw, "application/pdf")}


AUTH = {"Authorization": "Bearer test-key"}


# ---------------------------------------------------------------- accept
async def test_dengbao_pdf_accepted_returns_task_id(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "wren_chat_api.app.looks_like_dengbao", lambda raw: True
    )
    app, repo = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract", headers=AUTH,
            files=upload_pdf(),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["status"] == "processing"
    task_id = body["data"]["taskId"]
    uuid.UUID(task_id)  # well-formed
    row = repo.rows[task_id]
    assert row["file_bytes"] == _PDF_MAGIC_BYTES


async def test_identical_upload_reuses_in_flight_task(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "wren_chat_api.app.looks_like_dengbao", lambda raw: True
    )
    app, repo = make_app(tmp_path)
    async with client_for(app) as client:
        first = await client.post(
            "/v1/risk-assessment/extract", headers=AUTH, files=upload_pdf()
        )
        second = await client.post(
            "/v1/risk-assessment/extract", headers=AUTH, files=upload_pdf()
        )
    assert first.json()["data"]["taskId"] == second.json()["data"]["taskId"]
    assert len(repo.rows) == 1


async def test_succeeded_hash_served_from_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "wren_chat_api.app.looks_like_dengbao", lambda raw: True
    )
    repo = FakeRepo()
    seeded = repo._seed(_PDF_MAGIC_BYTES, "succeeded", result={
        "reportType": "dengbao", "detail": _REPORT,
    })
    app, _ = make_app(tmp_path, repo)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract", headers=AUTH, files=upload_pdf()
        )
        view = await client.get(
            f"/v1/report-tasks/{response.json()['data']['taskId']}",
            headers=AUTH,
        )
    assert response.json()["data"]["taskId"] == str(seeded["task_id"])
    assert len(repo.rows) == 1  # no new row, no re-run
    assert view.json()["status"] == "succeeded"
    assert view.json()["result"]["detail"] == _REPORT


async def test_non_dengbao_pdf_is_business_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "wren_chat_api.app.looks_like_dengbao", lambda raw: False
    )
    app, repo = make_app(tmp_path)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract", headers=AUTH, files=upload_pdf()
        )
    assert response.status_code == 200  # gateway convention
    body = response.json()
    assert body["code"] == 422
    assert body["data"] is None
    assert not repo.rows


async def test_dengbao_disabled_is_business_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "wren_chat_api.app.looks_like_dengbao", lambda raw: True
    )
    app, repo = make_app(tmp_path, dengbao=False)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/risk-assessment/extract", headers=AUTH, files=upload_pdf()
        )
    assert response.status_code == 200
    assert response.json()["code"] == 422
    assert not repo.rows


# ------------------------------------------------------------------- GET
async def test_get_task_lifecycle_views(tmp_path):
    app, repo = make_app(tmp_path)
    row = repo._seed(_PDF_MAGIC_BYTES, "pending")
    task_id = str(row["task_id"])
    async with client_for(app) as client:
        processing = await client.get(
            f"/v1/report-tasks/{task_id}", headers=AUTH
        )
        assert processing.status_code == 200
        body = processing.json()
        assert body["status"] == "processing"
        assert body["result"] is None and body["error"] is None

        repo.rows[task_id].update(
            status="failed", error_code="UPSTREAM_FAILED",
            error_message="上游模型服务异常，请稍后重试。",
            completed_at=datetime.now(timezone.utc),
        )
        failed = await client.get(
            f"/v1/report-tasks/{task_id}", headers=AUTH
        )
        assert failed.json()["status"] == "failed"
        assert failed.json()["error"]["code"] == "UPSTREAM_FAILED"

        repo.rows[task_id].update(
            status="succeeded", error_code=None, error_message=None,
            result={"reportType": "dengbao", "detail": _REPORT},
        )
        done = await client.get(f"/v1/report-tasks/{task_id}", headers=AUTH)
        assert done.json()["status"] == "succeeded"
        assert done.json()["result"] == {
            "reportType": "dengbao", "detail": _REPORT,
        }


async def test_get_unknown_or_malformed_task_id_is_404(tmp_path):
    app, _ = make_app(tmp_path)
    async with client_for(app) as client:
        unknown = await client.get(
            "/v1/report-tasks/" + str(uuid.uuid4()), headers=AUTH
        )
        malformed = await client.get(
            "/v1/report-tasks/not-a-uuid", headers=AUTH
        )
        unauth = await client.get(f"/v1/report-tasks/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "REPORT_TASK_NOT_FOUND"
    assert malformed.status_code == 404
    assert unauth.status_code == 401


# ------------------------------------------------------------- detector
def test_detector_rejects_non_pdf():
    assert looks_like_dengbao(b"plain text") is False


# --------------------------------------------------------------- worker
def _worker_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://docling.test", transport=httpx.MockTransport(handler)
    )


async def test_worker_processes_task_to_success(monkeypatch):
    captured: dict = {}

    def fake_extract(pdf_path, docling_json):  # sync: runs in a thread
        captured["pdf_path"] = pdf_path
        captured["docling_json"] = docling_json
        return {"report": _REPORT, "files": {}, "duration_seconds": 0.1}

    monkeypatch.setattr("report_postproc.dengbao_extract", fake_extract)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["preset"] == "standard"
        return httpx.Response(200, json={"docling_json": {"schema": "x"}})

    repo = FakeRepo()
    row = repo._seed(_PDF_MAGIC_BYTES, "running")
    async with _worker_client(handler) as client:
        await _process_one(dict(row), repo=repo, client=client)

    stored = repo.rows[str(row["task_id"])]
    assert stored["status"] == "succeeded"
    assert stored["result"] == {
        "reportType": "dengbao", "detail": _REPORT,
    }
    assert captured["docling_json"] == {"schema": "x"}
    assert captured["pdf_path"].endswith(".pdf")


async def test_worker_marks_upstream_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    repo = FakeRepo()
    row = repo._seed(_PDF_MAGIC_BYTES, "running")
    async with _worker_client(handler) as client:
        await _process_one(dict(row), repo=repo, client=client)

    stored = repo.rows[str(row["task_id"])]
    assert stored["status"] == "failed"
    assert stored["error_code"] == "UPSTREAM_FAILED"
