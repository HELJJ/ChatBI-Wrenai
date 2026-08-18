"""FastAPI application assembly: routes, auth, errors, and lifespan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypedDict

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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
)
from wren_chat_api.db import (
    apply_migrations,
    create_app_pool,
    create_checkpoint_pool,
    default_migrations_dir,
)
from wren_chat_api.errors import ChatServiceError, InternalError
from wren_chat_api.executor import BoundedWrenExecutor
from wren_chat_api.leases import LeaseRepository
from wren_chat_api.metrics import (
    REQUEST_LATENCY,
    REQUESTS,
    metrics_response,
)
from wren_chat_api.recovery import run_recovery_loop

logger = logging.getLogger(__name__)

_INVALID_REQUEST_MESSAGE = (
    "请求参数不合法，请检查会话 ID 与问题内容（问题不超过 4000 字符）。"
)


class AppOverrides(TypedDict, total=False):
    """Test seams replacing production wiring inside create_app."""

    chat_service: Any
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
        if "chat_service" in overrides
        else _production_lifespan(resolved_settings)
    )
    app = FastAPI(title="Wren Chat API", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.chat_service = overrides.get("chat_service")
    app.state.readiness = overrides.get("readiness") or _default_readiness

    def get_chat_service(request: Request) -> Any:
        service = request.app.state.chat_service
        if service is None:
            raise RuntimeError("chat service not initialized")
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
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error=ErrorBody(
                    code="INVALID_REQUEST",
                    message=_INVALID_REQUEST_MESSAGE,
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

        app.state.chat_service = chat_service
        app.state.readiness = readiness
        logger.info("wren chat api started")
        try:
            yield
        finally:
            stop_event.set()
            try:
                await asyncio.shield(recovery_task)
            except Exception:
                logger.warning("recovery loop exited with error", exc_info=True)
            executor.shutdown()
            await checkpoint_pool.close()
            await app_pool.close()
            logger.info("wren chat api stopped")

    return lifespan
