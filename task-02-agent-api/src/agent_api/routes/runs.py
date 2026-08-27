"""Authenticated HTTP routes for durable asynchronous runs."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Body, Depends, Header, Path, Request

from search_agent.contracts import OpaqueId

from ..ports import IdempotencyKey
from ..schemas import RunAcceptedResponse, RunStatusResponse, RunSubmitRequest
from ..security import AuthenticatedApiKey
from ..services import InvalidRequest, RunService
from .common import ERROR_RESPONSES, authenticate_request


def build_run_router() -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post(
        "/sessions/{session_id}/runs",
        response_model=RunAcceptedResponse,
        status_code=202,
        responses=ERROR_RESPONSES,
        tags=["runs"],
    )
    async def submit_run(
        http_request: Request,
        session_id: Annotated[OpaqueId, Path()],
        request: Annotated[RunSubmitRequest, Body()],
        principal: Annotated[AuthenticatedApiKey, Depends(_write_principal)],
        idempotency_key: Annotated[
            IdempotencyKey,
            Header(alias="Idempotency-Key", description="Tenant-scoped retry key"),
        ],
    ) -> RunAcceptedResponse:
        return await _service(http_request).submit(
            tenant_id=principal.tenant_id,
            session_id=session_id,
            idempotency_key=_single_idempotency_key(http_request, idempotency_key),
            query=request.query,
        )

    @router.get(
        "/runs/{run_id}",
        response_model=RunStatusResponse,
        responses=ERROR_RESPONSES,
        tags=["runs"],
    )
    async def get_run(
        http_request: Request,
        run_id: Annotated[OpaqueId, Path()],
        principal: Annotated[AuthenticatedApiKey, Depends(_read_principal)],
    ) -> RunStatusResponse:
        return await _service(http_request).get(
            tenant_id=principal.tenant_id,
            run_id=run_id,
        )

    return router


async def _write_principal(request: Request) -> AuthenticatedApiKey:
    service = _service(request)
    return await authenticate_request(
        request,
        required_scope="runs:write",
        now=service.now(),
    )


async def _read_principal(request: Request) -> AuthenticatedApiKey:
    service = _service(request)
    return await authenticate_request(
        request,
        required_scope="runs:read",
        now=service.now(),
    )


def _single_idempotency_key(
    request: Request, validated: IdempotencyKey
) -> IdempotencyKey:
    values = request.headers.getlist("idempotency-key")
    if len(values) != 1:
        raise InvalidRequest from None
    return validated


def _service(request: Request) -> RunService:
    return cast(RunService, request.app.state.run_service)
