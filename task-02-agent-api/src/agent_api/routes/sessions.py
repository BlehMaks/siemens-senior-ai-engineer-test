"""Authenticated HTTP routes for tenant-owned sessions."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, Depends, Path, Query, Request, Response

from search_agent.contracts import OpaqueId

from ..schemas import (
    CreateSessionRequest,
    DeletionResponse,
    ErrorEnvelope,
    PageCursor,
    SessionListResponse,
    SessionResponse,
)
from ..security import ApiKeyManager, AuthenticatedApiKey
from ..services import SessionService
from ..storage import SessionRecord

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope}
    for code in (400, 401, 403, 404, 409, 422, 429, 500, 503)
}


def build_session_router() -> APIRouter:
    router = APIRouter(prefix="/v1/sessions")

    @router.post(
        "",
        response_model=SessionResponse,
        status_code=201,
        responses=ERROR_RESPONSES,
        tags=["sessions"],
    )
    async def create_session(
        http_request: Request,
        request: Annotated[CreateSessionRequest, Body()],
        principal: Annotated[AuthenticatedApiKey, Depends(_write_principal)],
    ) -> SessionResponse:
        service = _service(http_request)
        record = await service.create(
            tenant_id=principal.tenant_id, label=request.label
        )
        return _public_session(record)

    @router.get(
        "",
        response_model=SessionListResponse,
        responses=ERROR_RESPONSES,
        tags=["sessions"],
    )
    async def list_sessions(
        http_request: Request,
        principal: Annotated[AuthenticatedApiKey, Depends(_read_principal)],
        cursor: Annotated[PageCursor | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> SessionListResponse:
        service = _service(http_request)
        records, next_cursor = await service.list(
            tenant_id=principal.tenant_id,
            limit=limit,
            cursor=cursor,
        )
        return SessionListResponse(
            items=tuple(_public_session(record) for record in records),
            next_cursor=next_cursor,
        )

    @router.get(
        "/{session_id}",
        response_model=SessionResponse,
        responses=ERROR_RESPONSES,
        tags=["sessions"],
    )
    async def get_session(
        http_request: Request,
        session_id: Annotated[OpaqueId, Path()],
        principal: Annotated[AuthenticatedApiKey, Depends(_read_principal)],
    ) -> SessionResponse:
        service = _service(http_request)
        return _public_session(
            await service.get(tenant_id=principal.tenant_id, session_id=session_id)
        )

    @router.delete(
        "/{session_id}",
        status_code=204,
        response_model=None,
        responses=ERROR_RESPONSES,
        tags=["sessions"],
    )
    async def delete_session(
        http_request: Request,
        session_id: Annotated[OpaqueId, Path()],
        principal: Annotated[AuthenticatedApiKey, Depends(_write_principal)],
    ) -> Response:
        service = _service(http_request)
        await service.delete(tenant_id=principal.tenant_id, session_id=session_id)
        return Response(status_code=204)

    @router.delete(
        "/{session_id}/memory",
        response_model=DeletionResponse,
        responses=ERROR_RESPONSES,
        tags=["memory"],
    )
    async def delete_memory(
        http_request: Request,
        session_id: Annotated[OpaqueId, Path()],
        principal: Annotated[AuthenticatedApiKey, Depends(_memory_principal)],
    ) -> DeletionResponse:
        service = _service(http_request)
        deleted_count = await service.delete_memory(
            tenant_id=principal.tenant_id, session_id=session_id
        )
        return DeletionResponse(
            deleted_count=deleted_count,
            completed_at=service.now(),
        )

    return router


async def _authenticate(request: Request, required_scope: str) -> AuthenticatedApiKey:
    values = request.headers.getlist("authorization")
    authorization = values[0] if len(values) == 1 else None
    manager = cast(ApiKeyManager, request.app.state.auth_manager)
    return await manager.authenticate(
        authorization=authorization,
        required_scope=required_scope,
        now=_service(request).now(),
    )


# Route dependencies authenticate before FastAPI validates handler inputs.
async def _read_principal(request: Request) -> AuthenticatedApiKey:
    return await _authenticate(request, "sessions:read")


async def _write_principal(request: Request) -> AuthenticatedApiKey:
    return await _authenticate(request, "sessions:write")


async def _memory_principal(request: Request) -> AuthenticatedApiKey:
    return await _authenticate(request, "memory:delete")


def _service(request: Request) -> SessionService:
    return cast(SessionService, request.app.state.session_service)


def _public_session(record: SessionRecord) -> SessionResponse:
    return SessionResponse(
        session_id=record.session_id,
        label=record.label,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
