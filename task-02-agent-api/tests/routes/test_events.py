from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openapi_contract import build_contract_app

from agent_api.app import create_app
from agent_api.ports import RunState, RunSubmission
from agent_api.storage import (
    SessionRecord,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    TenantRecord,
)

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
CORRELATION_ID = "corr-event-client-0001"


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


@dataclass(frozen=True)
class EventContext:
    app: FastAPI
    client: AsyncClient
    database_path: Path


@pytest_asyncio.fixture
async def event_context(tmp_path: Path) -> AsyncIterator[EventContext]:
    database_path = tmp_path / "events.sqlite3"
    app = create_app(
        database_path=database_path,
        pepper_provider=FixedPepper(),
        clock=lambda: NOW,
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        yield EventContext(app=app, client=client, database_path=database_path)


async def seed_tenant(
    context: EventContext,
    *,
    tenant_id: str,
    scopes: tuple[str, ...],
    run_id: str | None = None,
) -> str:
    await SQLiteTenantRepository(context.database_path).put(
        TenantRecord(tenant_id=tenant_id, created_at=NOW)
    )
    if run_id is not None:
        await SQLiteSessionRepository(context.database_path).put(
            SessionRecord(
                tenant_id=tenant_id,
                session_id="session-one",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await SQLiteRunRepository(context.database_path).create(
            RunSubmission(
                tenant_id=tenant_id,
                session_id="session-one",
                run_id=run_id,
                idempotency_key="request-key-one",
                query="Find the public documented answer.",
                created_at=NOW,
            )
        )
    generated = await context.app.state.auth_manager.create(
        tenant_id=tenant_id,
        scopes=scopes,
        now=NOW,
    )
    return f"Bearer {generated.plaintext}"


def headers(
    authorization: str | None, *, last_event_id: str | None = None
) -> list[tuple[str, str]]:
    values = [("X-Correlation-ID", CORRELATION_ID)]
    if authorization is not None:
        values.append(("Authorization", authorization))
    if last_event_id is not None:
        values.append(("Last-Event-ID", last_event_id))
    return values


async def cancel_run(context: EventContext) -> None:
    cancelled = await SQLiteRunRepository(context.database_path).request_cancellation(
        tenant_id="tenant-one",
        run_id="run-one",
        at=NOW + timedelta(seconds=2),
    )
    assert cancelled.changed
    assert cancelled.run is not None
    assert cancelled.run.state is RunState.CANCELLED


def assert_error(payload: object, *, code: str, message: str) -> None:
    assert payload == {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": CORRELATION_ID,
            "retryable": code == "unavailable",
            "field_issues": [],
        }
    }


@pytest.mark.asyncio
async def test_operational_event_route_matches_frozen_contract(
    event_context: EventContext,
) -> None:
    operational = event_context.app.openapi()["paths"]["/v1/runs/{run_id}/events"]
    frozen = build_contract_app().openapi()["paths"]["/v1/runs/{run_id}/events"]

    assert operational == frozen


@pytest.mark.asyncio
async def test_stream_resumes_after_cursor_and_terminal_event_closes(
    event_context: EventContext,
) -> None:
    authorization = await seed_tenant(
        event_context,
        tenant_id="tenant-one",
        scopes=("runs:read",),
        run_id="run-one",
    )
    await cancel_run(event_context)

    response = await event_context.client.get(
        "/v1/runs/run-one/events",
        headers=headers(authorization, last_event_id="1"),
    )

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == CORRELATION_ID
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.content.startswith(b"id: 2\nevent: run.cancelled\n")
    assert b"id: 1\n" not in response.content


@pytest.mark.asyncio
async def test_invalid_and_duplicate_resume_cursor_return_safe_422(
    event_context: EventContext,
) -> None:
    authorization = await seed_tenant(
        event_context,
        tenant_id="tenant-one",
        scopes=("runs:read",),
        run_id="run-one",
    )
    invalid = await event_context.client.get(
        "/v1/runs/run-one/events",
        headers=headers(authorization, last_event_id="0"),
    )
    duplicate_headers = headers(authorization, last_event_id="1")
    duplicate_headers.append(("Last-Event-ID", "2"))
    duplicate = await event_context.client.get(
        "/v1/runs/run-one/events",
        headers=duplicate_headers,
    )

    for response in (invalid, duplicate):
        assert response.status_code == 422
        assert_error(
            response.json(),
            code="invalid_request",
            message="Request validation failed.",
        )


@pytest.mark.asyncio
async def test_authentication_precedes_resume_header_validation(
    event_context: EventContext,
) -> None:
    response = await event_context.client.get(
        "/v1/runs/run-one/events",
        headers=headers(None, last_event_id="0"),
    )

    assert response.status_code == 401
    assert_error(
        response.json(),
        code="unauthenticated",
        message="Authentication failed.",
    )


@pytest.mark.asyncio
async def test_scope_and_tenant_boundaries_are_enforced_before_streaming(
    event_context: EventContext,
) -> None:
    owner = await seed_tenant(
        event_context,
        tenant_id="tenant-one",
        scopes=("runs:read",),
        run_id="run-one",
    )
    foreign = await seed_tenant(
        event_context,
        tenant_id="tenant-foreign",
        scopes=("runs:read",),
    )
    wrong_scope = await seed_tenant(
        event_context,
        tenant_id="tenant-write-only",
        scopes=("runs:write",),
    )
    await cancel_run(event_context)

    hidden = (
        await event_context.client.get(
            "/v1/runs/run-one/events", headers=headers(foreign)
        ),
        await event_context.client.get(
            "/v1/runs/run-missing/events", headers=headers(owner)
        ),
    )
    for response in hidden:
        assert response.status_code == 404
        assert_error(response.json(), code="not_found", message="Run was not found.")

    forbidden = await event_context.client.get(
        "/v1/runs/run-one/events", headers=headers(wrong_scope)
    )
    assert forbidden.status_code == 403
    assert_error(forbidden.json(), code="forbidden", message="Forbidden.")


@pytest.mark.asyncio
async def test_corrupted_first_event_maps_to_safe_unavailable(
    event_context: EventContext,
) -> None:
    authorization = await seed_tenant(
        event_context,
        tenant_id="tenant-one",
        scopes=("runs:read",),
        run_id="run-one",
    )
    with sqlite3.connect(event_context.database_path) as connection:
        connection.execute(
            "UPDATE run_events SET payload = ? WHERE run_id = ? AND sequence = ?",
            ("{}", "run-one", 1),
        )

    response = await event_context.client.get(
        "/v1/runs/run-one/events", headers=headers(authorization)
    )

    assert response.status_code == 503
    assert_error(
        response.json(),
        code="unavailable",
        message="Service is temporarily unavailable.",
    )
    assert "stored" not in response.text
