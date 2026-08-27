from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import AnyHttpUrl

from agent_api.app import create_app
from agent_api.storage import (
    SQLiteTenantRepository,
    TenantRecord,
    reflection_repository,
)
from search_agent.contracts import TerminalState
from search_agent.memory import CompletionEvidence, ReflectionUsage, RunReflection

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
CORRELATION_ID = "corr-client-0001"
MISSING_SESSION_ID = "session-missing00000000000000000000"
NON_JSON_CURSOR = "YWJjZGVmZ2g"


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


class SessionContext:
    def __init__(
        self, *, app: FastAPI, client: AsyncClient, database_path: Path
    ) -> None:
        self.app = app
        self.client = client
        self.database_path = database_path


def _clock() -> datetime:
    return NOW


@pytest_asyncio.fixture
async def session_context(tmp_path: Path) -> SessionContext:
    database_path = tmp_path / "sessions.sqlite3"
    app = create_app(
        database_path=database_path,
        pepper_provider=FixedPepper(),
        clock=_clock,
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        yield SessionContext(app=app, client=client, database_path=database_path)


async def _authorization(
    context: SessionContext,
    *,
    tenant_id: str,
    scopes: tuple[str, ...],
) -> str:
    await SQLiteTenantRepository(context.database_path).put(
        TenantRecord(tenant_id=tenant_id, created_at=NOW)
    )
    generated = await context.app.state.auth_manager.create(
        tenant_id=tenant_id,
        scopes=scopes,
        now=NOW,
    )
    return f"Bearer {generated.plaintext}"


def _headers(authorization: str | None) -> dict[str, str]:
    headers = {"X-Correlation-ID": CORRELATION_ID}
    if authorization is not None:
        headers["Authorization"] = authorization
    return headers


def _assert_error(
    payload: object,
    *,
    code: str,
    message: str,
    correlation_id: str = CORRELATION_ID,
    retryable: bool = False,
) -> None:
    assert payload == {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": correlation_id,
            "retryable": retryable,
            "field_issues": [],
        }
    }


def _reflection(*, tenant_id: str, session_id: str, run_id: str) -> RunReflection:
    return RunReflection(
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
        requested_outcome="Find the public Siemens report.",
        actions=(),
        failures=(),
        recovery_steps=(),
        completion_evidence=(
            CompletionEvidence(
                evidence_id="ev-public",
                source_url=AnyHttpUrl("https://example.com/report"),
            ),
        ),
        unresolved_items=(),
        outcome=TerminalState.COMPLETED,
        usage=ReflectionUsage(
            elapsed_seconds=0,
            iterations=0,
            search_queries=0,
            pages=0,
            failed_pages=0,
            raw_bytes_reserved=0,
            decoded_bytes=0,
            model_calls=0,
            model_attempts=0,
            tokens=0,
        ),
    )


@pytest.mark.asyncio
async def test_authenticated_crud_and_memory_lifecycle(
    session_context: SessionContext,
) -> None:
    client = session_context.client
    authorization = await _authorization(
        session_context,
        tenant_id="tenant-one",
        scopes=("sessions:read", "sessions:write", "memory:delete"),
    )

    created = await client.post(
        "/v1/sessions",
        json={"label": "Alpha"},
        headers=_headers(authorization),
    )
    assert created.status_code == 201
    session = created.json()
    assert session == {
        "session_id": session["session_id"],
        "label": "Alpha",
        "created_at": "2026-08-27T10:00:00Z",
        "updated_at": "2026-08-27T10:00:00Z",
    }

    fetched = await client.get(
        f"/v1/sessions/{session['session_id']}",
        headers=_headers(authorization),
    )
    assert fetched.status_code == 200
    assert fetched.json() == session

    listed = await client.get("/v1/sessions", headers=_headers(authorization))
    assert listed.status_code == 200
    assert listed.json() == {"items": [session], "next_cursor": None}

    memory = reflection_repository(session_context.database_path)
    memory.put(
        _reflection(
            tenant_id="tenant-one",
            session_id=session["session_id"],
            run_id="run-one",
        )
    )
    memory.close()

    deleted_memory = await client.delete(
        f"/v1/sessions/{session['session_id']}/memory",
        headers=_headers(authorization),
    )
    assert deleted_memory.status_code == 200
    assert deleted_memory.json() == {
        "deleted_count": 1,
        "completed_at": "2026-08-27T10:00:00Z",
    }

    deleted_memory_again = await client.delete(
        f"/v1/sessions/{session['session_id']}/memory",
        headers=_headers(authorization),
    )
    assert deleted_memory_again.status_code == 200
    assert deleted_memory_again.json() == {
        "deleted_count": 0,
        "completed_at": "2026-08-27T10:00:00Z",
    }

    still_exists = await client.get(
        f"/v1/sessions/{session['session_id']}",
        headers=_headers(authorization),
    )
    assert still_exists.status_code == 200
    assert still_exists.json() == session

    deleted = await client.delete(
        f"/v1/sessions/{session['session_id']}",
        headers=_headers(authorization),
    )
    assert deleted.status_code == 204
    assert deleted.content == b""

    deleted_again = await client.delete(
        f"/v1/sessions/{session['session_id']}",
        headers=_headers(authorization),
    )
    assert deleted_again.status_code == 404
    _assert_error(
        deleted_again.json(),
        code="not_found",
        message="Session was not found.",
    )


@pytest.mark.asyncio
async def test_two_tenant_boundaries_return_identical_not_found(
    session_context: SessionContext,
) -> None:
    client = session_context.client
    owner = await _authorization(
        session_context,
        tenant_id="tenant-owner",
        scopes=("sessions:read", "sessions:write", "memory:delete"),
    )
    foreign = await _authorization(
        session_context,
        tenant_id="tenant-foreign",
        scopes=("sessions:read", "sessions:write", "memory:delete"),
    )
    created = await client.post(
        "/v1/sessions",
        json={"label": "Owner"},
        headers=_headers(owner),
    )
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    missing = await client.get(
        f"/v1/sessions/{MISSING_SESSION_ID}",
        headers=_headers(owner),
    )
    foreign_get = await client.get(
        f"/v1/sessions/{session_id}",
        headers=_headers(foreign),
    )
    foreign_delete = await client.delete(
        f"/v1/sessions/{session_id}",
        headers=_headers(foreign),
    )
    foreign_memory = await client.delete(
        f"/v1/sessions/{session_id}/memory",
        headers=_headers(foreign),
    )

    for response in (missing, foreign_get, foreign_delete, foreign_memory):
        assert response.status_code == 404
        _assert_error(
            response.json(),
            code="not_found",
            message="Session was not found.",
        )


@pytest.mark.asyncio
async def test_session_pagination_is_stable_and_bounded(
    session_context: SessionContext,
) -> None:
    client = session_context.client
    authorization = await _authorization(
        session_context,
        tenant_id="tenant-one",
        scopes=("sessions:read", "sessions:write"),
    )
    created_ids: list[str] = []
    for index in range(101):
        created = await client.post(
            "/v1/sessions",
            json={"label": f"Session {index:03d}"},
            headers=_headers(authorization),
        )
        assert created.status_code == 201
        created_ids.append(created.json()["session_id"])

    first_page = await client.get(
        "/v1/sessions?limit=1", headers=_headers(authorization)
    )
    assert first_page.status_code == 200
    page_one = first_page.json()
    assert page_one["items"] == [
        {
            "session_id": min(created_ids),
            "label": page_one["items"][0]["label"],
            "created_at": "2026-08-27T10:00:00Z",
            "updated_at": "2026-08-27T10:00:00Z",
        }
    ]
    assert isinstance(page_one["next_cursor"], str)

    second_page = await client.get(
        f"/v1/sessions?limit=1&cursor={page_one['next_cursor']}",
        headers=_headers(authorization),
    )
    assert second_page.status_code == 200
    page_two = second_page.json()
    assert len(page_two["items"]) == 1
    assert page_two["items"][0]["session_id"] != page_one["items"][0]["session_id"]

    full_page = await client.get(
        "/v1/sessions?limit=100",
        headers=_headers(authorization),
    )
    assert full_page.status_code == 200
    payload = full_page.json()
    assert len(payload["items"]) == 100
    assert isinstance(payload["next_cursor"], str)

    final_page = await client.get(
        f"/v1/sessions?limit=100&cursor={payload['next_cursor']}",
        headers=_headers(authorization),
    )
    assert final_page.status_code == 200
    assert len(final_page.json()["items"]) == 1
    assert final_page.json()["next_cursor"] is None


@pytest.mark.asyncio
async def test_invalid_limit_cursor_and_id_are_rejected_safely(
    session_context: SessionContext,
) -> None:
    client = session_context.client
    authorization = await _authorization(
        session_context,
        tenant_id="tenant-one",
        scopes=("sessions:read", "sessions:write"),
    )

    for query in ("/v1/sessions?limit=0", "/v1/sessions?limit=101"):
        response = await client.get(query, headers=_headers(authorization))
        assert response.status_code == 422
        _assert_error(
            response.json(),
            code="invalid_request",
            message="Request validation failed.",
        )

    invalid_cursor = await client.get(
        f"/v1/sessions?cursor={NON_JSON_CURSOR}",
        headers=_headers(authorization),
    )
    assert invalid_cursor.status_code == 422
    _assert_error(
        invalid_cursor.json(),
        code="invalid_request",
        message="Request validation failed.",
    )

    invalid_id = await client.get(
        "/v1/sessions/not-an-opaque id",
        headers=_headers(authorization),
    )
    assert invalid_id.status_code == 422
    _assert_error(
        invalid_id.json(),
        code="invalid_request",
        message="Request validation failed.",
    )

    encoded_slash = await client.get(
        "/v1/sessions/session%2Fescape",
        headers=_headers(authorization),
    )
    assert encoded_slash.status_code == 404
    _assert_error(
        encoded_slash.json(),
        code="not_found",
        message="Resource was not found.",
    )

    method_not_allowed = await client.patch(
        "/v1/sessions",
        headers=_headers(authorization),
    )
    assert method_not_allowed.status_code == 405
    _assert_error(
        method_not_allowed.json(),
        code="invalid_request",
        message="Method is not allowed.",
    )
    assert method_not_allowed.headers["allow"] in {"GET", "POST"}


@pytest.mark.asyncio
async def test_authentication_precedes_request_validation(
    session_context: SessionContext,
) -> None:
    client = session_context.client
    responses = (
        await client.post(
            "/v1/sessions",
            json={"label": ""},
            headers=_headers(None),
        ),
        await client.get(
            "/v1/sessions?limit=0",
            headers=_headers(None),
        ),
        await client.get(
            "/v1/sessions/not-an-opaque id",
            headers=_headers(None),
        ),
    )
    for response in responses:
        assert response.status_code == 401
        _assert_error(
            response.json(),
            code="unauthenticated",
            message="Authentication failed.",
        )


@pytest.mark.asyncio
async def test_missing_and_wrong_scopes_map_to_401_and_403(
    session_context: SessionContext,
) -> None:
    client = session_context.client
    session_write = await _authorization(
        session_context,
        tenant_id="tenant-one",
        scopes=("sessions:write",),
    )
    session_read = await _authorization(
        session_context,
        tenant_id="tenant-two",
        scopes=("sessions:read",),
    )
    memory_only = await _authorization(
        session_context,
        tenant_id="tenant-three",
        scopes=("memory:delete",),
    )

    unauthenticated = await client.get("/v1/sessions", headers=_headers(None))
    assert unauthenticated.status_code == 401
    _assert_error(
        unauthenticated.json(),
        code="unauthenticated",
        message="Authentication failed.",
    )

    forbidden_read = await client.get(
        "/v1/sessions",
        headers=_headers(session_write),
    )
    assert forbidden_read.status_code == 403
    _assert_error(
        forbidden_read.json(),
        code="forbidden",
        message="Forbidden.",
    )

    created = await client.post(
        "/v1/sessions",
        json={},
        headers=_headers(session_write),
    )
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    forbidden_delete = await client.delete(
        f"/v1/sessions/{session_id}",
        headers=_headers(session_read),
    )
    assert forbidden_delete.status_code == 403
    _assert_error(
        forbidden_delete.json(),
        code="forbidden",
        message="Forbidden.",
    )

    forbidden_memory = await client.delete(
        f"/v1/sessions/{session_id}/memory",
        headers=_headers(session_write),
    )
    assert forbidden_memory.status_code == 403
    _assert_error(
        forbidden_memory.json(),
        code="forbidden",
        message="Forbidden.",
    )

    not_found_memory = await client.delete(
        f"/v1/sessions/{MISSING_SESSION_ID}/memory",
        headers=_headers(memory_only),
    )
    assert not_found_memory.status_code == 404
    _assert_error(
        not_found_memory.json(),
        code="not_found",
        message="Session was not found.",
    )


@pytest.mark.asyncio
async def test_concurrent_create_returns_distinct_session_ids(
    session_context: SessionContext,
) -> None:
    client = session_context.client
    authorization = await _authorization(
        session_context,
        tenant_id="tenant-one",
        scopes=("sessions:write",),
    )
    responses = await asyncio.gather(
        *(
            client.post(
                "/v1/sessions",
                json={"label": f"Session {index}"},
                headers=_headers(authorization),
            )
            for index in range(8)
        )
    )

    assert all(response.status_code == 201 for response in responses)
    session_ids = {response.json()["session_id"] for response in responses}
    assert len(session_ids) == len(responses)
