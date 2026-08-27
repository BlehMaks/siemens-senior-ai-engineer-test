"""FastAPI application assembly with safe public error boundaries."""

from __future__ import annotations

import base64
import secrets
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from search_agent.contracts import OpaqueId

from .observability import (
    OperationalTelemetry,
    ReadinessProbe,
    SQLiteReadinessProbe,
)
from .ports import IdempotencyConflictError
from .routes import (
    build_event_router,
    build_health_router,
    build_internal_task_router,
    build_run_router,
    build_session_router,
)
from .schemas import ErrorCode, ErrorDetail, ErrorEnvelope
from .security import (
    ApiKeyAuthError,
    ApiKeyManager,
    EnvPepperProvider,
    LimitConfig,
    PepperProvider,
    QuotaExceeded,
    QuotaLimiter,
    RequestBodyLimitMiddleware,
    RequestTooLarge,
    SQLiteQuotaLimiter,
)
from .services import (
    EventStreamService,
    InvalidRequest,
    RunNotFound,
    RunService,
    SessionNotFound,
    SessionService,
    SessionUnavailable,
)
from .storage import (
    CloudTasksWorkQueue,
    FirestoreEventRepository,
    FirestoreRunRepository,
    SQLiteAuditRepository,
    SQLiteEventRepository,
    SQLiteKeyHashRepository,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteWorkQueue,
    StorageError,
    TaskDeliveryAuthError,
    migrate,
)
from .workers import LocalWorker, QueueReceiver, RunExecutor, worker_lifespan

_OPAQUE_ID = TypeAdapter(OpaqueId)


async def _correlation_header(
    request: Request,
    correlation_id: Annotated[
        OpaqueId | None,
        Header(alias="X-Correlation-ID", description="Opaque request correlation ID"),
    ] = None,
) -> None:
    values = request.headers.getlist("x-correlation-id")
    checked = (
        _new_correlation_id()
        if correlation_id is None or len(values) != 1
        else correlation_id
    )
    request.state.correlation_id = checked


class _CorrelationHeaderMiddleware:
    """Attach the validated request ID to every HTTP response implementation."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_correlation(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                correlation_id = cast(
                    str | None, scope.get("state", {}).get("correlation_id")
                )
                if correlation_id is not None:
                    headers["X-Correlation-ID"] = correlation_id
                path = cast(str, scope.get("path", ""))
                if path == "/v1" or path.startswith("/v1/"):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self._app(scope, receive, send_with_correlation)


def create_app(
    *,
    database_path: Path,
    pepper_provider: PepperProvider | None = None,
    clock: Callable[[], datetime] | None = None,
    session_id_factory: Callable[[], str] | None = None,
    run_id_factory: Callable[[], str] | None = None,
    run_executor: RunExecutor | None = None,
    worker_shutdown_seconds: float = 5.0,
    limit_config: LimitConfig | None = None,
    quota_limiter: QuotaLimiter | None = None,
    telemetry: OperationalTelemetry | None = None,
    readiness_probe: ReadinessProbe | None = None,
    run_repository: SQLiteRunRepository | FirestoreRunRepository | None = None,
    event_repository: SQLiteEventRepository | FirestoreEventRepository | None = None,
    work_queue: SQLiteWorkQueue | CloudTasksWorkQueue | None = None,
    production_environment: bool = False,
    run_state_backend: str = "sqlite",
    queue_backend: str = "sqlite",
    queue_delivery_path: str = "/internal/tasks/run-delivery",
    task_delivery_enabled: bool = False,
) -> FastAPI:
    provider = EnvPepperProvider() if pepper_provider is None else pepper_provider
    now = _utc_now if clock is None else clock
    limits = LimitConfig() if limit_config is None else limit_config
    _validate_authoritative_runtime(
        production_environment=production_environment,
        run_state_backend=run_state_backend,
        queue_backend=queue_backend,
        has_run_repository=run_repository is not None,
        has_event_repository=event_repository is not None,
        has_work_queue=work_queue is not None,
        has_run_executor=run_executor is not None,
        task_delivery_enabled=task_delivery_enabled,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await migrate(database_path)
        durable_runs = (
            SQLiteRunRepository(database_path)
            if run_repository is None
            else run_repository
        )
        durable_queue = (
            SQLiteWorkQueue(database_path) if work_queue is None else work_queue
        )
        durable_events = (
            SQLiteEventRepository(database_path)
            if event_repository is None
            else event_repository
        )
        limiter = (
            SQLiteQuotaLimiter(database_path, limits)
            if quota_limiter is None
            else quota_limiter
        )
        app.state.quota_limiter = limiter
        runtime_telemetry = (
            OperationalTelemetry(
                pseudonym_key=provider.pepper(),
                audit=SQLiteAuditRepository(database_path),
            )
            if telemetry is None
            else telemetry
        )
        app.state.telemetry = runtime_telemetry
        app.state.readiness_probe = (
            SQLiteReadinessProbe(database_path)
            if readiness_probe is None
            else readiness_probe
        )
        app.state.auth_manager = ApiKeyManager(
            SQLiteKeyHashRepository(database_path), provider
        )
        app.state.session_service = SessionService(
            SQLiteSessionRepository(database_path),
            clock=now,
            id_factory=session_id_factory,
        )
        app.state.run_service = RunService(
            durable_runs,
            durable_queue,
            clock=now,
            run_id_factory=run_id_factory,
            limiter=limiter,
        )
        app.state.event_stream_service = EventStreamService(
            durable_runs,
            durable_events,
            clock=now,
        )
        app.state.work_queue = durable_queue
        worker = (
            None
            if run_executor is None
            else LocalWorker(
                repository=durable_runs,
                # Signed delivery calls process() directly; receive() is reached
                # only by the local polling lifespan, which is disabled below.
                queue=cast(QueueReceiver, durable_queue),
                executor=run_executor,
                worker_id="worker-local",
                clock=now,
                cancellation_drain_seconds=worker_shutdown_seconds,
                limiter=limiter,
                telemetry=runtime_telemetry,
            )
        )
        app.state.internal_worker = worker
        async with worker_lifespan(
            None if task_delivery_enabled else worker,
            shutdown_seconds=worker_shutdown_seconds,
        ):
            yield

    app = FastAPI(
        title="Research Agent API",
        version="1.0.0",
        dependencies=[Depends(_correlation_header)],
        lifespan=lifespan,
    )
    app.state.clock = now
    app.include_router(build_health_router(clock=now))
    app.include_router(build_session_router())
    app.include_router(build_run_router())
    app.include_router(build_event_router())
    if task_delivery_enabled:
        app.include_router(build_internal_task_router(path=queue_delivery_path))
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=limits.max_request_bytes,
    )
    app.add_middleware(_CorrelationHeaderMiddleware)
    app.add_exception_handler(ApiKeyAuthError, _auth_error)
    app.add_exception_handler(SessionNotFound, _not_found)
    app.add_exception_handler(RunNotFound, _run_not_found)
    app.add_exception_handler(InvalidRequest, _invalid_request)
    app.add_exception_handler(IdempotencyConflictError, _conflict)
    app.add_exception_handler(SessionUnavailable, _unavailable)
    app.add_exception_handler(StorageError, _unavailable)
    app.add_exception_handler(QuotaExceeded, _quota_exceeded)
    app.add_exception_handler(RequestTooLarge, _request_too_large)
    app.add_exception_handler(TaskDeliveryAuthError, _task_delivery_auth_error)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _internal_error)
    return app


async def _auth_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(ApiKeyAuthError, exc)
    code = (
        ErrorCode.UNAUTHENTICATED if error.status_code == 401 else ErrorCode.FORBIDDEN
    )
    message = "Authentication failed." if error.status_code == 401 else "Forbidden."
    return _error_response(request, error.status_code, code, message)


async def _not_found(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return _error_response(request, 404, ErrorCode.NOT_FOUND, "Session was not found.")


async def _run_not_found(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return _error_response(request, 404, ErrorCode.NOT_FOUND, "Run was not found.")


async def _conflict(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return _error_response(
        request, 409, ErrorCode.CONFLICT, "Idempotency key conflicts with a run."
    )


async def _invalid_request(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return _error_response(
        request, 422, ErrorCode.INVALID_REQUEST, "Request validation failed."
    )


async def _task_delivery_auth_error(
    request: Request, exc: Exception
) -> JSONResponse:
    del exc
    return _error_response(
        request, 401, ErrorCode.UNAUTHENTICATED, "Authentication failed."
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(StarletteHTTPException, exc)
    if error.status_code == 404:
        return _error_response(
            request,
            404,
            ErrorCode.NOT_FOUND,
            "Resource was not found.",
            headers=error.headers,
        )
    if error.status_code == 405:
        return _error_response(
            request,
            405,
            ErrorCode.INVALID_REQUEST,
            "Method is not allowed.",
            headers=error.headers,
        )
    return _error_response(
        request,
        error.status_code,
        ErrorCode.INVALID_REQUEST if error.status_code < 500 else ErrorCode.INTERNAL,
        "Request could not be completed.",
        headers=error.headers,
    )


async def _unavailable(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return _error_response(
        request,
        503,
        ErrorCode.UNAVAILABLE,
        "Service is temporarily unavailable.",
        retryable=True,
    )


async def _quota_exceeded(request: Request, exc: Exception) -> JSONResponse:
    error = cast(QuotaExceeded, exc)
    return _error_response(
        request,
        429,
        ErrorCode.RATE_LIMITED,
        "Request quota was exceeded.",
        retryable=True,
        headers={"Retry-After": str(error.retry_after)},
    )


async def _request_too_large(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return _error_response(
        request,
        413,
        ErrorCode.INVALID_REQUEST,
        "Request body was too large.",
    )


async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    del exc
    clock = cast(Callable[[], datetime], request.app.state.clock)
    signals = cast(OperationalTelemetry, request.app.state.telemetry)
    signals.unexpected_error(
        correlation_id=_correlation_id(request),
        at=clock(),
    )
    return _error_response(
        request,
        500,
        ErrorCode.INTERNAL,
        "Request could not be completed.",
    )


def _error_response(
    request: Request,
    status_code: int,
    code: ErrorCode,
    message: str,
    *,
    retryable: bool = False,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=message,
            correlation_id=_correlation_id(request),
            retryable=retryable,
        )
    )
    response_headers = {} if headers is None else dict(headers)
    response_headers["X-Correlation-ID"] = envelope.error.correlation_id
    if request.url.path == "/v1" or request.url.path.startswith("/v1/"):
        response_headers["Cache-Control"] = "no-store"
    return JSONResponse(
        content=envelope.model_dump(mode="json"),
        status_code=status_code,
        headers=response_headers,
    )


def _correlation_id(request: Request) -> OpaqueId:
    stored = getattr(request.state, "correlation_id", None)
    if stored is not None:
        try:
            return _OPAQUE_ID.validate_python(stored, strict=True)
        except ValidationError:
            pass
    values = request.headers.getlist("x-correlation-id")
    if len(values) == 1:
        try:
            return _OPAQUE_ID.validate_python(values[0], strict=True)
        except ValidationError:
            pass
    return _new_correlation_id()


def _new_correlation_id() -> OpaqueId:
    encoded = base64.b32encode(secrets.token_bytes(10)).decode().lower().rstrip("=")
    return _OPAQUE_ID.validate_python(f"corr-{encoded}", strict=True)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_authoritative_runtime(
    *,
    production_environment: bool,
    run_state_backend: str,
    queue_backend: str,
    has_run_repository: bool,
    has_event_repository: bool,
    has_work_queue: bool,
    has_run_executor: bool,
    task_delivery_enabled: bool,
) -> None:
    if not production_environment:
        return
    if run_state_backend != "firestore":
        raise ValueError("production_environment requires Firestore run state")
    if queue_backend != "cloud_tasks":
        raise ValueError("production_environment requires Cloud Tasks delivery")
    if not has_run_repository:
        raise ValueError("production_environment requires an injected run repository")
    if not has_event_repository:
        raise ValueError("production_environment requires an injected event repository")
    if not has_work_queue:
        raise ValueError("production_environment requires an injected work queue")
    if has_run_executor and not task_delivery_enabled:
        raise ValueError("production worker requires signed task delivery")
