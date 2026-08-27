"""Authenticated resumable SSE delivery for tenant-owned runs."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, Path, Request
from starlette.responses import StreamingResponse

from search_agent.contracts import OpaqueId

from ..schemas import LastEventId, RunEvent, parse_last_event_id
from ..security import AuthenticatedApiKey, QuotaLimiter, SSEPermit
from ..services import EventStreamService, InvalidRequest
from .common import ERROR_RESPONSES, authenticate_request


def build_event_router() -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get(
        "/runs/{run_id}/events",
        response_model=RunEvent,
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Resumable typed run-event stream",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            },
            **ERROR_RESPONSES,
        },
        tags=["runs"],
    )
    async def stream_events(
        http_request: Request,
        run_id: Annotated[OpaqueId, Path()],
        principal: Annotated[AuthenticatedApiKey, Depends(_read_principal)],
        last_event_id: Annotated[
            LastEventId | None,
            Header(alias="Last-Event-ID", description="Last processed event sequence"),
        ] = None,
    ) -> StreamingResponse:
        cursor = _cursor(http_request, last_event_id)
        limiter = _limiter(http_request)
        permit = await limiter.acquire_sse(
            tenant_id=principal.tenant_id,
            key_id=principal.key_id,
            at=_service(http_request).now(),
        )
        try:
            body = await _service(http_request).open_stream(
                tenant_id=principal.tenant_id,
                run_id=run_id,
                after_sequence=cursor,
                disconnected=http_request.is_disconnected,
            )
        except BaseException:
            await limiter.release_sse(permit)
            raise
        return StreamingResponse(
            _limited_stream(
                body,
                limiter=limiter,
                permit=permit,
                clock=_service(http_request).now,
            ),
            media_type="text/event-stream",
        )

    return router


async def _read_principal(request: Request) -> AuthenticatedApiKey:
    service = _service(request)
    return await authenticate_request(
        request,
        required_scope="runs:read",
        now=service.now(),
    )


def _cursor(request: Request, validated: LastEventId | None) -> int:
    values = request.headers.getlist("last-event-id")
    if len(values) > 1:
        raise InvalidRequest from None
    raw = values[0] if values else None
    try:
        parsed = parse_last_event_id(raw)
    except ValueError:
        raise InvalidRequest from None
    if validated is not None and raw != validated:
        raise InvalidRequest from None
    return 0 if parsed is None else parsed


def _service(request: Request) -> EventStreamService:
    return cast(EventStreamService, request.app.state.event_stream_service)


def _limiter(request: Request) -> QuotaLimiter:
    return cast(QuotaLimiter, request.app.state.quota_limiter)


async def _limited_stream(
    body: AsyncIterator[bytes],
    *,
    limiter: QuotaLimiter,
    permit: SSEPermit,
    clock: Callable[[], datetime],
) -> AsyncIterator[bytes]:
    try:
        async for chunk in body:
            try:
                if not await limiter.renew_sse(permit, at=clock()):
                    return
            except Exception:
                return
            yield chunk
    finally:
        with contextlib.suppress(Exception):
            await limiter.release_sse(permit)


__all__ = ["build_event_router"]
