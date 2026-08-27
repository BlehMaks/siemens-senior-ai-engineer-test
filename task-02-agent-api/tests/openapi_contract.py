"""Contract-only FastAPI fixture; no endpoint here is executable application code."""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import Body, Depends, FastAPI, Header, Path, Query
from starlette.responses import StreamingResponse

from agent_api.ports import IdempotencyKey
from agent_api.schemas import (
    CancellationResponse,
    CreateSessionRequest,
    DeletionResponse,
    ErrorEnvelope,
    HealthResponse,
    LastEventId,
    PageCursor,
    RunAcceptedResponse,
    RunEvent,
    RunStatusResponse,
    RunSubmitRequest,
    SessionListResponse,
    SessionResponse,
)
from search_agent.contracts import OpaqueId

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope}
    for code in (400, 401, 403, 404, 409, 413, 422, 429, 500, 503)
}


def _contract_only() -> NoReturn:
    raise AssertionError("OpenAPI contract fixture is not an operational API")


async def _correlation_header(
    correlation_id: Annotated[
        OpaqueId | None,
        Header(alias="X-Correlation-ID", description="Opaque request correlation ID"),
    ] = None,
) -> None:
    del correlation_id


def build_contract_app() -> FastAPI:
    app = FastAPI(
        title="Research Agent API",
        version="1.0.0",
        dependencies=[Depends(_correlation_header)],
    )

    @app.get(
        "/health/live",
        response_model=HealthResponse,
        responses=ERROR_RESPONSES,
        tags=["health"],
    )
    async def live() -> HealthResponse:
        _contract_only()

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses=ERROR_RESPONSES,
        tags=["health"],
    )
    async def ready() -> HealthResponse:
        _contract_only()

    @app.post(
        "/v1/sessions",
        response_model=SessionResponse,
        status_code=201,
        responses=ERROR_RESPONSES,
        tags=["sessions"],
    )
    async def create_session(
        request: Annotated[CreateSessionRequest, Body()],
    ) -> SessionResponse:
        del request
        _contract_only()

    @app.get(
        "/v1/sessions",
        response_model=SessionListResponse,
        responses=ERROR_RESPONSES,
        tags=["sessions"],
    )
    async def list_sessions(
        cursor: Annotated[PageCursor | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> SessionListResponse:
        del cursor, limit
        _contract_only()

    @app.get(
        "/v1/sessions/{session_id}",
        response_model=SessionResponse,
        responses=ERROR_RESPONSES,
        tags=["sessions"],
    )
    async def get_session(
        session_id: Annotated[OpaqueId, Path()],
    ) -> SessionResponse:
        del session_id
        _contract_only()

    @app.delete(
        "/v1/sessions/{session_id}",
        status_code=204,
        response_model=None,
        responses=ERROR_RESPONSES,
        tags=["sessions"],
    )
    async def delete_session(session_id: Annotated[OpaqueId, Path()]) -> None:
        del session_id
        _contract_only()

    @app.delete(
        "/v1/sessions/{session_id}/memory",
        response_model=DeletionResponse,
        responses=ERROR_RESPONSES,
        tags=["memory"],
    )
    async def delete_memory(
        session_id: Annotated[OpaqueId, Path()],
    ) -> DeletionResponse:
        del session_id
        _contract_only()

    @app.post(
        "/v1/sessions/{session_id}/runs",
        response_model=RunAcceptedResponse,
        status_code=202,
        responses=ERROR_RESPONSES,
        tags=["runs"],
    )
    async def submit_run(
        session_id: Annotated[OpaqueId, Path()],
        request: Annotated[RunSubmitRequest, Body()],
        idempotency_key: Annotated[
            IdempotencyKey,
            Header(alias="Idempotency-Key", description="Tenant-scoped retry key"),
        ],
    ) -> RunAcceptedResponse:
        del session_id, request, idempotency_key
        _contract_only()

    @app.get(
        "/v1/runs/{run_id}",
        response_model=RunStatusResponse,
        responses=ERROR_RESPONSES,
        tags=["runs"],
    )
    async def get_run(run_id: Annotated[OpaqueId, Path()]) -> RunStatusResponse:
        del run_id
        _contract_only()

    @app.post(
        "/v1/runs/{run_id}/cancel",
        response_model=CancellationResponse,
        status_code=202,
        responses=ERROR_RESPONSES,
        tags=["runs"],
    )
    async def cancel_run(run_id: Annotated[OpaqueId, Path()]) -> CancellationResponse:
        del run_id
        _contract_only()

    @app.get(
        "/v1/runs/{run_id}/events",
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
        run_id: Annotated[OpaqueId, Path()],
        last_event_id: Annotated[
            LastEventId | None,
            Header(alias="Last-Event-ID", description="Last processed event sequence"),
        ] = None,
    ) -> StreamingResponse:
        del run_id, last_event_id
        _contract_only()

    return app
