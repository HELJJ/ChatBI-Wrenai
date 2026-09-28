"""FastAPI application assembly: routes, auth, errors, and lifespan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypedDict

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from wren_chat_api.agent import build_chat_graph
from wren_chat_api.audit import AuditRepository
from wren_chat_api.audited_query import AuditedQuery
from wren_chat_api.auth import ServiceKeyAuth
from wren_chat_api.chat import ChatService
from wren_chat_api.config import Settings
from wren_chat_api.contracts import (
    ChatRequest,
    ChatResponse,
    ErrorBody,
    ErrorResponse,
    PentestExtractResponse,
    ReportTaskAcceptedData,
    ReportTaskView,
    RiskAssessmentAcceptResponse,
    RiskAssessmentExtractResponse,
    RiskAssessmentFailure,
    RiskSelfCheckRequest,
    RiskSelfCheckResponse,
    SecurityAnalysisResponse,
)
from wren_chat_api.db import (
    apply_migrations,
    create_app_pool,
    create_checkpoint_pool,
    default_migrations_dir,
)
from wren_chat_api.dengbao_worker import (
    parse_task_id,
    run_report_task_loop,
    task_view,
)
from wren_chat_api.docling_detail import PentestDoclingChannel
from wren_chat_api.errors import (
    ChatServiceError,
    InternalError,
    InvalidRiskFile,
    ReportTaskNotFound,
)
from wren_chat_api.executor import BoundedWrenExecutor
from wren_chat_api.leases import LeaseRepository
from wren_chat_api.metrics import (
    REQUEST_LATENCY,
    REQUESTS,
    metrics_response,
)
from wren_chat_api.pentest_extract import (
    PentestExtractService,
    validate_pentest_upload,
)
from wren_chat_api.recovery import run_recovery_loop
from wren_chat_api.report_tasks import (
    ReportTaskRepository,
    looks_like_dengbao,
)
from wren_chat_api.risk_assessment import (
    RiskAssessmentService,
    validate_risk_upload,
)
from wren_chat_api.risk_selfcheck import RiskSelfCheckService
from wren_chat_api.security_analysis import AnalysisService, validate_report

logger = logging.getLogger(__name__)

_INVALID_REQUEST_MESSAGE = (
    "请求参数不合法，请检查会话 ID 与问题内容（问题不超过 4000 字符）。"
)
_ANALYSIS_INVALID_REQUEST_MESSAGE = (
    "请求参数不合法，请以 multipart/form-data 上传名为 file 的 .md 报告文件。"
)
_PENTEST_INVALID_REQUEST_MESSAGE = (
    "请求参数不合法，请以 multipart/form-data 上传名为 file 的 .pdf 渗透测试记录单。"
)
_RISK_ASSESSMENT_INVALID_REQUEST_MESSAGE = (
    "请求参数不合法，请以 multipart/form-data 上传名为 file 的 "
    ".doc/.docx 风险评估报告。"
)
_RISK_SELFCHECK_INVALID_REQUEST_MESSAGE = (
    "请求参数不合法：list 需为 1-50 项，每项含 id、component、version 及 "
    "1-50 条漏洞描述（每条不超过 4000 字符）。"
)
_GENERIC_INVALID_REQUEST_MESSAGE = "请求参数不合法，请检查请求内容。"


class AppOverrides(TypedDict, total=False):
    """Test seams replacing production wiring inside create_app."""

    chat_service: Any
    analysis_service: Any
    pentest_service: Any
    risk_selfcheck_service: Any
    risk_assessment_service: Any
    pentest_docling_channel: Any
    report_task_repo: Any
    readiness: Callable[[], Awaitable[None]]


def create_app(
    settings: Settings | None = None,
    overrides: AppOverrides | None = None,
) -> FastAPI:
    """Assemble the service app, optionally with test overrides."""
    resolved_settings = settings or Settings()
    overrides = overrides or {}
    auth = ServiceKeyAuth(resolved_settings)

    lifespan = (
        None
        if (
            "chat_service" in overrides
            or "analysis_service" in overrides
            or "pentest_service" in overrides
            or "risk_selfcheck_service" in overrides
            or "risk_assessment_service" in overrides
            or "pentest_docling_channel" in overrides
            or "report_task_repo" in overrides
        )
        else _production_lifespan(resolved_settings)
    )
    app = FastAPI(title="Wren Chat API", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.chat_service = overrides.get("chat_service")
    app.state.analysis_service = overrides.get("analysis_service")
    app.state.pentest_service = overrides.get("pentest_service")
    app.state.risk_selfcheck_service = overrides.get("risk_selfcheck_service")
    app.state.risk_assessment_service = overrides.get("risk_assessment_service")
    # Additive docling channel: constructed here (lightweight — the heavy
    # converter lives in the parsing service); None keeps the wire format
    # byte-identical to the pre-channel contract.
    app.state.pentest_docling_channel = overrides.get("pentest_docling_channel")
    if app.state.pentest_docling_channel is None:
        app.state.pentest_docling_channel = (
            PentestDoclingChannel(settings=resolved_settings)
            if resolved_settings.pentest_docling_enabled
            else None
        )
    # Dengbao task repository: production wiring lives in the lifespan
    # (needs the pool); tests inject a fake via overrides.
    app.state.report_task_repo = overrides.get("report_task_repo")
    app.state.readiness = overrides.get("readiness") or _default_readiness

    def get_chat_service(request: Request) -> Any:
        service = request.app.state.chat_service
        if service is None:
            raise RuntimeError("chat service not initialized")
        return service

    def get_analysis_service(request: Request) -> Any:
        service = request.app.state.analysis_service
        if service is None:
            raise RuntimeError("analysis service not initialized")
        return service

    def get_pentest_service(request: Request) -> Any:
        service = request.app.state.pentest_service
        if service is None:
            raise RuntimeError("pentest service not initialized")
        return service

    def get_risk_selfcheck_service(request: Request) -> Any:
        service = request.app.state.risk_selfcheck_service
        if service is None:
            raise RuntimeError("risk self-check service not initialized")
        return service

    def get_risk_assessment_service(request: Request) -> Any:
        service = request.app.state.risk_assessment_service
        if service is None:
            raise RuntimeError("risk assessment service not initialized")
        return service

    @app.post(
        "/v1/chat",
        response_model=ChatResponse,
        dependencies=[Depends(auth)],
    )
    async def chat(
        request: ChatRequest,
        service: Any = Depends(get_chat_service),
    ) -> ChatResponse:
        route = "/v1/chat"
        with REQUEST_LATENCY.labels(route=route).time():
            try:
                response = await service.ask(request)
            except ChatServiceError as exc:
                REQUESTS.labels(route=route, status=str(exc.http_status)).inc()
                raise
            except Exception as exc:
                # Unknown failures must still produce the stable envelope;
                # Starlette re-raises from Exception-keyed handlers, so map
                # them to a typed error instead of a catch-all handler.
                logger.error("unhandled chat error", exc_info=True)
                REQUESTS.labels(route=route, status="500").inc()
                raise InternalError(cause=exc) from exc
        REQUESTS.labels(route=route, status="200").inc()
        return response

    @app.post(
        "/v1/security-report/analysis",
        response_model=SecurityAnalysisResponse,
        response_model_exclude_none=True,
        dependencies=[Depends(auth)],
    )
    async def analyze_security_report(
        file: UploadFile = File(...),
        service: Any = Depends(get_analysis_service),
    ) -> SecurityAnalysisResponse:
        route = "/v1/security-report/analysis"
        with REQUEST_LATENCY.labels(route=route).time():
            try:
                raw = await file.read()
                content = validate_report(
                    file.filename,
                    raw,
                    resolved_settings.max_report_bytes,
                )
                response = await service.analyze(file.filename, content)
            except ChatServiceError as exc:
                REQUESTS.labels(route=route, status=str(exc.http_status)).inc()
                raise
            except Exception as exc:
                logger.error("unhandled analysis error", exc_info=True)
                REQUESTS.labels(route=route, status="500").inc()
                raise InternalError(cause=exc) from exc
        REQUESTS.labels(route=route, status="200").inc()
        return response

    @app.post(
        "/v1/pentest-report/extract",
        response_model=PentestExtractResponse,
        response_model_exclude_none=True,
        dependencies=[Depends(auth)],
    )
    async def extract_pentest_report(
        request: Request,
        file: UploadFile = File(...),
        service: Any = Depends(get_pentest_service),
    ) -> PentestExtractResponse:
        route = "/v1/pentest-report/extract"
        with REQUEST_LATENCY.labels(route=route).time():
            try:
                raw = await file.read()
                validate_pentest_upload(file.filename, raw)
                response = await service.extract(file.filename, raw)
                # Additive docling channel (fail-open): enrichment runs only
                # after the primary extraction succeeded, and any failure
                # inside the channel degrades to detail=None (omitted from
                # the wire) without touching the primary fields. The route
                # guards too — the promise must not depend on the channel
                # implementation never raising.
                channel = request.app.state.pentest_docling_channel
                if channel is not None:
                    try:
                        detail = await channel.extract_detail(file.filename, raw)
                    except Exception:
                        logger.warning(
                            "pentest docling channel degraded", exc_info=True
                        )
                        detail = None
                    response = response.model_copy(update={"detail": detail})
            except ChatServiceError as exc:
                REQUESTS.labels(route=route, status=str(exc.http_status)).inc()
                raise
            except Exception as exc:
                logger.error("unhandled pentest extraction error", exc_info=True)
                REQUESTS.labels(route=route, status="500").inc()
                raise InternalError(cause=exc) from exc
        REQUESTS.labels(route=route, status="200").inc()
        return response

    @app.post(
        "/v1/risk-assessment/extract",
        response_model=RiskAssessmentExtractResponse,
        dependencies=[Depends(auth)],
    )
    async def extract_risk_assessment(
        request: Request,
        file: UploadFile = File(...),
        service: Any = Depends(get_risk_assessment_service),
    ) -> Response:
        route = "/v1/risk-assessment/extract"
        with REQUEST_LATENCY.labels(route=route).time():
            try:
                raw = await file.read()
                # Additive dengbao branch: a PDF upload is classified by its
                # first pages and, when it is a 等保测评报告, accepted as an
                # async task (the docling conversion alone takes ~14 min for
                # a 500-page report — far beyond any sane synchronous call).
                # Everything else — the .doc/.docx risk-assessment path —
                # is unchanged.
                if (file.filename or "").lower().endswith(".pdf"):
                    return await _accept_dengbao(request, file.filename, raw)
                validate_risk_upload(file.filename, raw)
                response = RiskAssessmentExtractResponse(
                    data=await service.extract(file.filename, raw)
                )
            except ChatServiceError as exc:
                # Business failures answer HTTP 200 with the endpoint's
                # business-code envelope (gateway convention); the metric
                # still records the business code so failure rates stay
                # visible. Transport-level failures (auth dependency,
                # malformed multipart) keep their real HTTP statuses.
                logger.warning(
                    "risk assessment extraction failed (%s): %s",
                    exc.code,
                    exc.internal_message,
                )
                REQUESTS.labels(route=route, status=str(exc.http_status)).inc()
                return JSONResponse(
                    status_code=200,
                    content=RiskAssessmentFailure(
                        code=exc.http_status,
                        message=exc.public_message,
                    ).model_dump(),
                )
            except Exception:
                logger.error("unhandled risk assessment error", exc_info=True)
                REQUESTS.labels(route=route, status="500").inc()
                return JSONResponse(
                    status_code=200,
                    content=RiskAssessmentFailure(
                        code=InternalError.http_status,
                        message=InternalError.public_message,
                    ).model_dump(),
                )
        REQUESTS.labels(route=route, status="200").inc()
        return response

    @app.post(
        "/v1/risk/self-check",
        response_model=RiskSelfCheckResponse,
        dependencies=[Depends(auth)],
    )
    async def risk_self_check(
        request: RiskSelfCheckRequest,
        service: Any = Depends(get_risk_selfcheck_service),
    ) -> RiskSelfCheckResponse:
        route = "/v1/risk/self-check"
        with REQUEST_LATENCY.labels(route=route).time():
            try:
                response = await service.check_batch(request)
            except ChatServiceError as exc:
                REQUESTS.labels(route=route, status=str(exc.http_status)).inc()
                raise
            except Exception as exc:
                logger.error("unhandled risk self-check error", exc_info=True)
                REQUESTS.labels(route=route, status="500").inc()
                raise InternalError(cause=exc) from exc
        REQUESTS.labels(route=route, status="200").inc()
        return response

    async def _accept_dengbao(
        request: Request, filename: str | None, raw: bytes
    ) -> Response:
        """Classify and accept a PDF as a dengbao task, or raise typed."""
        settings: Settings = request.app.state.settings
        repo = request.app.state.report_task_repo
        if not settings.risk_dengbao_enabled or repo is None:
            raise InvalidRiskFile("dengbao pdf branch disabled")
        if not raw.lstrip()[:1024].startswith(b"%PDF"):
            raise InvalidRiskFile("missing %PDF header")
        # Cheap text scan of the first pages; runs in a thread (pypdfium2).
        if not await asyncio.to_thread(looks_like_dengbao, raw):
            raise InvalidRiskFile(
                "pdf is not a 等级保护测评报告 (keywords not found)"
            )
        task = await repo.create_or_reuse(filename=filename or "report.pdf",
                                          raw=raw)
        logger.info(
            "dengbao task %s accepted for %s (%s)",
            task["task_id"], filename, task["reused"],
        )
        return JSONResponse(
            status_code=200,
            content=RiskAssessmentAcceptResponse(
                data=ReportTaskAcceptedData(taskId=str(task["task_id"]))
            ).model_dump(),
        )

    @app.get(
        "/v1/report-tasks/{task_id}",
        dependencies=[Depends(auth)],
    )
    async def get_report_task(task_id: str, request: Request) -> Response:
        """Poll one report task: processing / succeeded (with result) / failed."""
        route = "/v1/report-tasks"
        with REQUEST_LATENCY.labels(route=route).time():
            repo = request.app.state.report_task_repo
            if repo is None:
                raise RuntimeError("report task repository not initialized")
            parsed = parse_task_id(task_id)
            task = await repo.get(parsed) if parsed is not None else None
            if task is None:
                REQUESTS.labels(route=route, status="404").inc()
                raise ReportTaskNotFound()
            view = ReportTaskView(**task_view(task))
        REQUESTS.labels(route=route, status="200").inc()
        return JSONResponse(
            status_code=200,
            content=view.model_dump(),
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        try:
            await request.app.state.readiness()
        except Exception:
            logger.warning("readiness check failed", exc_info=True)
            return JSONResponse(
                status_code=503,
                content=ErrorResponse(
                    error=ErrorBody(
                        code="SERVICE_UNAVAILABLE",
                        message="服务尚未就绪，请稍后重试。",
                    )
                ).model_dump(),
            )
        return JSONResponse(status_code=200, content={"status": "ready"})

    @app.get("/metrics", dependencies=[Depends(auth)])
    async def metrics() -> Any:
        return metrics_response()

    @app.exception_handler(ChatServiceError)
    async def service_error_handler(
        request: Request,
        exc: ChatServiceError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=ErrorResponse(
                error=ErrorBody(code=exc.code, message=exc.public_message)
            ).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        messages_by_path = {
            "/v1/chat": _INVALID_REQUEST_MESSAGE,
            "/v1/security-report/analysis": _ANALYSIS_INVALID_REQUEST_MESSAGE,
            "/v1/pentest-report/extract": _PENTEST_INVALID_REQUEST_MESSAGE,
            "/v1/risk-assessment/extract": _RISK_ASSESSMENT_INVALID_REQUEST_MESSAGE,
            "/v1/risk/self-check": _RISK_SELFCHECK_INVALID_REQUEST_MESSAGE,
        }
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error=ErrorBody(
                    code="INVALID_REQUEST",
                    message=messages_by_path.get(
                        request.url.path,
                        _GENERIC_INVALID_REQUEST_MESSAGE,
                    ),
                )
            ).model_dump(),
        )

    return app


async def _default_readiness() -> None:
    """Placeholder readiness used when production wiring is overridden."""


def _production_lifespan(settings: Settings) -> Callable[[FastAPI], Any]:
    """Build the FastAPI lifespan wiring every production resource."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from langchain_core.messages import HumanMessage
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from wren.config import WrenConfig
        from wren_langchain import WrenToolkit

        app_pool = create_app_pool(settings)
        await app_pool.open()
        await app_pool.wait()
        await apply_migrations(app_pool, default_migrations_dir())

        checkpoint_pool = create_checkpoint_pool(settings)
        await checkpoint_pool.open()
        await checkpoint_pool.wait()
        checkpointer = AsyncPostgresSaver(checkpoint_pool)
        await checkpointer.setup()

        model = ChatOpenAI(
            model=settings.model,
            api_key=(
                settings.model_api_key.get_secret_value()
                if settings.model_api_key is not None
                else None
            ),
            base_url=settings.model_base_url,
            model_kwargs={
                "extra_body": {
                    "enable_thinking": settings.model_enable_thinking,
                }
            },
        )

        async def summarize(history: str, previous: str) -> str:
            prompt = (
                "Update the rolling summary of an earlier business-data "
                "conversation. Keep every durable fact (periods, metrics, "
                "numbers, filters).\n\n"
                f"Previous summary:\n{previous}\n\n"
                f"Conversation to fold in:\n{history}\n\n"
                "Return only the updated summary as plain text."
            )
            response = await model.ainvoke([HumanMessage(content=prompt)])
            return str(response.content)

        toolkit = WrenToolkit.from_project(
            settings.project_path,
            config=WrenConfig(strict_mode=True),
        )
        executor = BoundedWrenExecutor(
            workers=settings.wren_workers,
            queue_capacity=settings.wren_queue_capacity,
        )
        audit = AuditRepository(
            app_pool,
            max_sql_attempts=settings.max_sql_attempts,
        )
        leases = LeaseRepository(app_pool)
        audited_query = AuditedQuery(
            audit=audit,
            toolkit=toolkit,
            settings=settings,
            executor=executor,
            dialect=settings.sql_dialect,
        )
        graph = build_chat_graph(
            toolkit,
            model,
            summarize,
            checkpointer,
            settings,
        )
        chat_service = ChatService(
            leases=leases,
            audit=audit,
            graph=graph,
            audited_query=audited_query,
            settings=settings,
        )
        analysis_service = AnalysisService(model=model, settings=settings)
        app.state.risk_selfcheck_service = RiskSelfCheckService(
            model=model,
            settings=settings,
        )
        # Needs neither the database nor the chat graph: constructed from
        # settings alone (model credentials resolved inside).
        app.state.pentest_service = PentestExtractService(settings=settings)
        app.state.risk_assessment_service = RiskAssessmentService(
            model=model,
            settings=settings,
        )

        async def readiness() -> None:
            async with app_pool.connection() as conn:
                await conn.execute("SELECT 1")

        stop_event = asyncio.Event()
        recovery_task = asyncio.create_task(
            run_recovery_loop(
                stop_event=stop_event,
                pool=app_pool,
                interval_seconds=settings.recovery_interval_seconds,
                threshold_seconds=settings.interruption_threshold_seconds,
            )
        )

        # Dengbao report-task worker: one task at a time behind the
        # single-concurrency parsing service; startup resets rows left
        # 'running' by an unclean shutdown back to pending.
        report_repo = ReportTaskRepository(app_pool)
        app.state.report_task_repo = report_repo
        report_stop = asyncio.Event()
        report_worker = asyncio.create_task(
            run_report_task_loop(
                repo=report_repo,
                settings=settings,
                stop_event=report_stop,
            )
        )

        app.state.chat_service = chat_service
        app.state.analysis_service = analysis_service
        app.state.readiness = readiness
        logger.info("wren chat api started")
        try:
            yield
        finally:
            # Cancel the report worker rather than awaiting it: an
            # in-flight conversion can take ~14 minutes and must not block
            # shutdown. The row stays 'running'; the next startup's
            # reset_stale_running re-queues it.
            report_stop.set()
            report_worker.cancel()
            try:
                await report_worker
            except asyncio.CancelledError:
                pass
            stop_event.set()
            try:
                await asyncio.shield(recovery_task)
            except Exception:
                logger.warning("recovery loop exited with error", exc_info=True)
            channel = app.state.pentest_docling_channel
            if channel is not None:
                await channel.aclose()
            executor.shutdown()
            await checkpoint_pool.close()
            await app_pool.close()
            logger.info("wren chat api stopped")

    return lifespan
