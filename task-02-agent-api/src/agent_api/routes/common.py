"""Shared HTTP contract and authentication helpers for versioned routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from fastapi import Request

from ..schemas import ErrorEnvelope
from ..security import ApiKeyManager, AuthenticatedApiKey

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope}
    for code in (400, 401, 403, 404, 409, 422, 429, 500, 503)
}


async def authenticate_request(
    request: Request, *, required_scope: str, now: datetime
) -> AuthenticatedApiKey:
    values = request.headers.getlist("authorization")
    authorization = values[0] if len(values) == 1 else None
    manager = cast(ApiKeyManager, request.app.state.auth_manager)
    return await manager.authenticate(
        authorization=authorization,
        required_scope=required_scope,
        now=now,
    )
