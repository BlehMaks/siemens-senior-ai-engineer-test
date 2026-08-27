"""Shared HTTP contract and authentication helpers for versioned routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from fastapi import Request

from search_agent.contracts import OpaqueId

from ..observability import OperationalTelemetry
from ..schemas import ErrorEnvelope
from ..security import (
    ApiKeyAuthError,
    ApiKeyManager,
    AuthenticatedApiKey,
    QuotaLimiter,
    RequestTooLarge,
    request_too_large,
)

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope}
    for code in (400, 401, 403, 404, 409, 413, 422, 429, 500, 503)
}


async def authenticate_request(
    request: Request, *, required_scope: str, now: datetime
) -> AuthenticatedApiKey:
    values = request.headers.getlist("authorization")
    authorization = values[0] if len(values) == 1 else None
    manager = cast(ApiKeyManager, request.app.state.auth_manager)
    signals = telemetry(request)
    try:
        principal = await manager.authenticate(
            authorization=authorization,
            required_scope=required_scope,
            now=now,
        )
    except ApiKeyAuthError as error:
        signals.auth_outcome(
            outcome=error.code,
            correlation_id=correlation_id(request),
            tenant_id=error.tenant_id,
            at=now,
        )
        raise
    signals.auth_outcome(
        outcome="authenticated",
        correlation_id=correlation_id(request),
        tenant_id=principal.tenant_id,
        at=now,
    )
    limiter = cast(QuotaLimiter, request.app.state.quota_limiter)
    await limiter.admit_request(
        tenant_id=principal.tenant_id,
        key_id=principal.key_id,
        at=now,
    )
    if request_too_large(request):
        raise RequestTooLarge
    return principal


def correlation_id(request: Request) -> OpaqueId:
    return cast(OpaqueId, request.state.correlation_id)


def telemetry(request: Request) -> OperationalTelemetry:
    return cast(OperationalTelemetry, request.app.state.telemetry)
