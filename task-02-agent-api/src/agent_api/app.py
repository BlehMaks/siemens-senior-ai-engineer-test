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
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from search_agent.contracts import OpaqueId

from .ports import IdempotencyConflictError
from .routes import build_event_router, build_run_router, build_session_router
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
    SQLiteEventRepository,
    SQLiteKeyHashRepository,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteWorkQueue,
    StorageError,
    migrate,
)
from .workers import LocalWorker, RunExecutor, worker_lifespan

_OPAQUE_ID = TypeAdapter(OpaqueId)


async def _correlation_header(
    correlation_id: Annotated[
        OpaqueId | None,
        Header(alias="X-Correlation-ID", description="Opaque request correlation ID"),
    ] = None,
) -> None:
    del correlation_id


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
) -> FastAPI:
    provider = EnvPepperProvider() if pepper_provider is None else pepper_provider
    now = _utc_now if clock is None else clock
    limits = LimitConfig() if limit_config is None else limit_config

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await migrate(database_path)
        run_repository = SQLiteRunRepository(database_path)
        work_queue = SQLiteWorkQueue(database_path)
        limiter = (
            SQLiteQuotaLimiter(database_path, limits)
            if quota_limiter is None
            else quota_limiter
        )
        app.state.quota_limiter = limiter
        app.state.auth_manager = ApiKeyManager(
            SQLiteKeyHashRepository(database_path), provider
        )
        app.state.session_service = SessionService(
            SQLiteSessionRepository(database_path),
            clock=now,
            id_factory=session_id_factory,
        )
        app.state.run_service = RunService(
            run_repository,
            work_queue,
            clock=now,
            run_id_factory=run_id_factory,
            limiter=limiter,
        )
        app.state.event_stream_service = EventStreamService(
            run_repository,
            SQLiteEventRepository(database_path),
            clock=now,
        )
        worker = (
            None
            if run_executor is None
            else LocalWorker(
                repository=run_repository,
                queue=work_queue,
                executor=run_executor,
                worker_id="worker-local",
                clock=now,
                cancellation_drain_seconds=worker_shutdown_seconds,
                limiter=limiter,
            )
        )
        async with worker_lifespan(
            worker,
            shutdown_seconds=worker_shutdown_seconds,
        ):
            yield

    app = FastAPI(
        title="Research Agent API",
        version="1.0.0",
        dependencies=[Depends(_correlation_header)],
        lifespan=lifespan,
    )
    app.include_router(build_session_router())
    app.include_router(build_run_router())
    app.include_router(build_event_router())
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=limits.max_request_bytes,
    )
    app.add_exception_handler(ApiKeyAuthError, _auth_error)
    app.add_exception_handler(SessionNotFound, _not_found)
    app.add_exception_handler(RunNotFound, _run_not_found)
    app.add_exception_handler(InvalidRequest, _invalid_request)
    app.add_exception_handler(IdempotencyConflictError, _conflict)
    app.add_exception_handler(SessionUnavailable, _unavailable)
    app.add_exception_handler(StorageError, _unavailable)
    app.add_exception_handler(QuotaExceeded, _quota_exceeded)
    app.add_exception_handler(RequestTooLarge, _request_too_large)
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
    return JSONResponse(
        content=envelope.model_dump(mode="json"),
        status_code=status_code,
        headers=headers,
    )


def _correlation_id(request: Request) -> OpaqueId:
    values = request.headers.getlist("x-correlation-id")
    if len(values) == 1:
        try:
            return _OPAQUE_ID.validate_python(values[0], strict=True)
        except ValidationError:
            pass
    encoded = base64.b32encode(secrets.token_bytes(10)).decode().lower().rstrip("=")
    return _OPAQUE_ID.validate_python(f"corr-{encoded}", strict=True)


def _utc_now() -> datetime:
    return datetime.now(UTC)
