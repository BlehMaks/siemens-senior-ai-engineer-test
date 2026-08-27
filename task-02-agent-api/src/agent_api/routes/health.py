"""Public liveness and dependency-readiness probes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import cast

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from ..observability import OperationalTelemetry, ReadinessProbe
from ..schemas import ErrorEnvelope, HealthResponse, HealthState
from .common import correlation_id


def build_health_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/health/live",
        response_model=HealthResponse,
        responses={422: {"model": ErrorEnvelope}, 500: {"model": ErrorEnvelope}},
        tags=["health"],
    )
    async def live() -> HealthResponse:
        return HealthResponse(status=HealthState.OK, checked_at=clock())

    @router.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={
            422: {"model": ErrorEnvelope},
            500: {"model": ErrorEnvelope},
            503: {"model": HealthResponse},
        },
        tags=["health"],
    )
    async def ready(request: Request) -> HealthResponse | JSONResponse:
        checked_at = clock()
        try:
            is_ready = await cast(
                ReadinessProbe, request.app.state.readiness_probe
            ).ready()
        except Exception:
            is_ready = False
        cast(OperationalTelemetry, request.app.state.telemetry).readiness(
            ready=is_ready, at=checked_at
        )
        response = HealthResponse(
            status=HealthState.OK if is_ready else HealthState.NOT_READY,
            checked_at=checked_at,
        )
        if is_ready:
            return response
        return JSONResponse(
            response.model_dump(mode="json"),
            status_code=503,
            headers={"X-Correlation-ID": correlation_id(request)},
        )

    return router


__all__ = ["build_health_router"]
