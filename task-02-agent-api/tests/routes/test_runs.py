from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openapi_contract import build_contract_app

from agent_api.app import create_app
from agent_api.ports import EnqueueResult, WorkItem
from agent_api.services import RunService
from agent_api.storage import (
    SessionRecord,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    SQLiteWorkQueue,
    StorageError,
    TenantRecord,
)

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
CORRELATION_ID = "corr-run-client-0001"


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


class UnavailableQueue:
    async def enqueue(self, item: WorkItem) -> EnqueueResult:
        del item
        raise StorageError("private queue dependency detail")

    async def cancel(
        self,
        *,
        tenant_id: str,
        run_id: str,
        generation_id: str | None = None,
    ) -> int:
        del tenant_id, run_id, generation_id
        return 0


@dataclass(frozen=True)
class RunContext:
    app: FastAPI
    client: AsyncClient
    database_path: Path


@pytest_asyncio.fixture
async def run_context(tmp_path: Path) -> AsyncIterator[RunContext]:
    database_path = tmp_path / "runs.sqlite3"
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
        yield RunContext(app=app, client=client, database_path=database_path)


async def _tenant_key(
    context: RunContext,
    *,
    tenant_id: str,
    scopes: tuple[str, ...],
    session_ids: tuple[str, ...] = (),
) -> str:
    await SQLiteTenantRepository(context.database_path).put(
        TenantRecord(tenant_id=tenant_id, created_at=NOW)
    )
    sessions = SQLiteSessionRepository(context.database_path)
    for session_id in session_ids:
        await sessions.put(
            SessionRecord(
                tenant_id=tenant_id,
                session_id=session_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    generated = await context.app.state.auth_manager.create(
        tenant_id=tenant_id,
        scopes=scopes,
        now=NOW,
    )
    return f"Bearer {generated.plaintext}"


def _headers(authorization: str, key: str = "request-key-one") -> dict[str, str]:
    return {
        "Authorization": authorization,
        "Idempotency-Key": key,
        "X-Correlation-ID": CORRELATION_ID,
    }


def _assert_error(payload: object, *, code: str, message: str) -> None:
    assert payload == {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": CORRELATION_ID,
            "retryable": False,
            "field_issues": [],
        }
    }


@pytest.mark.asyncio
async def test_operational_run_routes_match_the_frozen_contract(
    run_context: RunContext,
) -> None:
    operational = run_context.app.openapi()["paths"]
    frozen = build_contract_app().openapi()["paths"]

    assert (
        operational["/v1/sessions/{session_id}/runs"]
        == frozen["/v1/sessions/{session_id}/runs"]
    )
    assert operational["/v1/runs/{run_id}"] == frozen["/v1/runs/{run_id}"]


@pytest.mark.asyncio
async def test_submit_persists_run_and_work_before_get(run_context: RunContext) -> None:
    authorization = await _tenant_key(
        run_context,
        tenant_id="tenant-one",
        scopes=("runs:read", "runs:write"),
        session_ids=("session-one",),
    )

    accepted = await run_context.client.post(
        "/v1/sessions/session-one/runs",
        json={"query": "Find the documented public answer."},
        headers=_headers(authorization),
    )
    assert accepted.status_code == 202
    body = accepted.json()
    assert body == {
        "session_id": "session-one",
        "run_id": body["run_id"],
        "state": "queued",
        "created_at": "2026-08-27T10:00:00Z",
    }

    stored = await SQLiteRunRepository(run_context.database_path).get(
        tenant_id="tenant-one", run_id=body["run_id"]
    )
    assert stored is not None
    assert stored.query == "Find the documented public answer."
    work = await SQLiteWorkQueue(run_context.database_path).receive(
        now=NOW, visibility_seconds=30
    )
    assert work is not None
    assert (work.tenant_id, work.run_id) == ("tenant-one", body["run_id"])

    status = await run_context.client.get(
        f"/v1/runs/{body['run_id']}",
        headers={
            "Authorization": authorization,
            "X-Correlation-ID": CORRELATION_ID,
        },
    )
    assert status.status_code == 200
    assert status.json() == {
        "session_id": "session-one",
        "run_id": body["run_id"],
        "state": "queued",
        "created_at": "2026-08-27T10:00:00Z",
        "updated_at": "2026-08-27T10:00:00Z",
        "terminal_at": None,
        "cancellation_requested": False,
        "answer": None,
        "failure": None,
    }


@pytest.mark.asyncio
async def test_concurrent_idempotent_submission_creates_one_run_and_work_item(
    run_context: RunContext,
) -> None:
    authorization = await _tenant_key(
        run_context,
        tenant_id="tenant-one",
        scopes=("runs:write",),
        session_ids=("session-one", "session-two"),
    )
    responses = await asyncio.gather(
        *(
            run_context.client.post(
                "/v1/sessions/session-one/runs",
                json={"query": "Find one stable answer."},
                headers=_headers(authorization),
            )
            for _ in range(8)
        )
    )
    assert all(response.status_code == 202 for response in responses)
    run_ids = {response.json()["run_id"] for response in responses}
    assert len(run_ids) == 1
    assert (
        len(
            await SQLiteRunRepository(run_context.database_path).list_session(
                tenant_id="tenant-one", session_id="session-one"
            )
        )
        == 1
    )

    queue = SQLiteWorkQueue(run_context.database_path)
    assert await queue.receive(now=NOW, visibility_seconds=30) is not None
    assert await queue.receive(now=NOW, visibility_seconds=30) is None

    different_query = await run_context.client.post(
        "/v1/sessions/session-one/runs",
        json={"query": "Find a different answer."},
        headers=_headers(authorization),
    )
    different_session = await run_context.client.post(
        "/v1/sessions/session-two/runs",
        json={"query": "Find one stable answer."},
        headers=_headers(authorization),
    )
    for conflict in (different_query, different_session):
        assert conflict.status_code == 409
        _assert_error(
            conflict.json(),
            code="conflict",
            message="Idempotency key conflicts with a run.",
        )


@pytest.mark.asyncio
async def test_run_routes_hide_foreign_and_absent_resources(
    run_context: RunContext,
) -> None:
    owner = await _tenant_key(
        run_context,
        tenant_id="tenant-owner",
        scopes=("runs:read", "runs:write"),
        session_ids=("session-owner",),
    )
    foreign = await _tenant_key(
        run_context,
        tenant_id="tenant-foreign",
        scopes=("runs:read", "runs:write"),
    )

    foreign_session = await run_context.client.post(
        "/v1/sessions/session-owner/runs",
        json={"query": "Find the owner answer."},
        headers=_headers(foreign),
    )
    absent_session = await run_context.client.post(
        "/v1/sessions/session-absent/runs",
        json={"query": "Find the owner answer."},
        headers=_headers(foreign),
    )
    assert foreign_session.status_code == absent_session.status_code == 404
    assert foreign_session.json() == absent_session.json()

    accepted = await run_context.client.post(
        "/v1/sessions/session-owner/runs",
        json={"query": "Find the owner answer."},
        headers=_headers(owner),
    )
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]
    read_headers = {
        "Authorization": foreign,
        "X-Correlation-ID": CORRELATION_ID,
    }
    foreign_run = await run_context.client.get(
        f"/v1/runs/{run_id}", headers=read_headers
    )
    absent_run = await run_context.client.get(
        "/v1/runs/run-absent", headers=read_headers
    )
    assert foreign_run.status_code == absent_run.status_code == 404
    assert foreign_run.json() == absent_run.json()
    assert run_id not in str(foreign_run.json())


@pytest.mark.asyncio
async def test_authentication_and_idempotency_header_fail_safely(
    run_context: RunContext,
) -> None:
    writer = await _tenant_key(
        run_context,
        tenant_id="tenant-writer",
        scopes=("runs:write",),
        session_ids=("session-one",),
    )
    reader = await _tenant_key(
        run_context,
        tenant_id="tenant-reader",
        scopes=("runs:read",),
    )

    missing_auth = await run_context.client.post(
        "/v1/sessions/not-an-opaque id/runs",
        json={"query": ""},
        headers={"X-Correlation-ID": CORRELATION_ID},
    )
    wrong_scope = await run_context.client.post(
        "/v1/sessions/not-an-opaque id/runs",
        json={"query": ""},
        headers=_headers(reader),
    )
    missing_get_auth = await run_context.client.get(
        "/v1/runs/not-an-opaque id",
        headers={"X-Correlation-ID": CORRELATION_ID},
    )
    wrong_get_scope = await run_context.client.get(
        "/v1/runs/not-an-opaque id",
        headers={
            "Authorization": writer,
            "X-Correlation-ID": CORRELATION_ID,
        },
    )
    assert missing_auth.status_code == 401
    assert missing_auth.json()["error"]["code"] == "unauthenticated"
    assert wrong_scope.status_code == 403
    assert wrong_scope.json()["error"]["code"] == "forbidden"
    assert missing_get_auth.status_code == 401
    assert missing_get_auth.json()["error"]["code"] == "unauthenticated"
    assert wrong_get_scope.status_code == 403
    assert wrong_get_scope.json()["error"]["code"] == "forbidden"

    missing_key = await run_context.client.post(
        "/v1/sessions/session-one/runs",
        json={"query": "Find the answer."},
        headers={
            "Authorization": writer,
            "X-Correlation-ID": CORRELATION_ID,
        },
    )
    malformed_key = await run_context.client.post(
        "/v1/sessions/session-one/runs",
        json={"query": "Find the answer."},
        headers=_headers(writer, "short"),
    )
    duplicate_key = await run_context.client.post(
        "/v1/sessions/session-one/runs",
        json={"query": "Find the answer."},
        headers=[
            ("Authorization", writer),
            ("Idempotency-Key", "request-key-one"),
            ("Idempotency-Key", "request-key-two"),
            ("X-Correlation-ID", CORRELATION_ID),
        ],
    )
    for invalid in (missing_key, malformed_key, duplicate_key):
        assert invalid.status_code == 422
        _assert_error(
            invalid.json(),
            code="invalid_request",
            message="Request validation failed.",
        )


@pytest.mark.asyncio
async def test_accepted_run_and_work_survive_app_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "restart.sqlite3"
    first = create_app(
        database_path=database_path,
        pepper_provider=FixedPepper(),
        clock=lambda: NOW,
    )
    async with (
        first.router.lifespan_context(first),
        AsyncClient(
            transport=ASGITransport(app=first), base_url="http://testserver"
        ) as client,
    ):
        context = RunContext(app=first, client=client, database_path=database_path)
        authorization = await _tenant_key(
            context,
            tenant_id="tenant-one",
            scopes=("runs:read", "runs:write"),
            session_ids=("session-one",),
        )
        accepted = await client.post(
            "/v1/sessions/session-one/runs",
            json={"query": "Find a restart-safe answer."},
            headers=_headers(authorization),
        )
        run_id = accepted.json()["run_id"]

    second = create_app(
        database_path=database_path,
        pepper_provider=FixedPepper(),
        clock=lambda: NOW,
    )
    async with (
        second.router.lifespan_context(second),
        AsyncClient(
            transport=ASGITransport(app=second), base_url="http://testserver"
        ) as client,
    ):
        status = await client.get(
            f"/v1/runs/{run_id}",
            headers={
                "Authorization": authorization,
                "X-Correlation-ID": CORRELATION_ID,
            },
        )
        work = await SQLiteWorkQueue(database_path).receive(
            now=NOW, visibility_seconds=30
        )

    assert status.status_code == 200
    assert status.json()["run_id"] == run_id
    assert work is not None and work.run_id == run_id


@pytest.mark.asyncio
async def test_queue_failure_returns_retryable_safe_error(
    run_context: RunContext,
) -> None:
    authorization = await _tenant_key(
        run_context,
        tenant_id="tenant-one",
        scopes=("runs:write",),
        session_ids=("session-one",),
    )
    run_context.app.state.run_service = RunService(
        SQLiteRunRepository(run_context.database_path),
        UnavailableQueue(),
        clock=lambda: NOW,
        run_id_factory=lambda: "run-one",
    )

    response = await run_context.client.post(
        "/v1/sessions/session-one/runs",
        json={"query": "Find the private dependency detail."},
        headers=_headers(authorization),
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "unavailable",
            "message": "Service is temporarily unavailable.",
            "correlation_id": CORRELATION_ID,
            "retryable": True,
            "field_issues": [],
        }
    }
    assert "private" not in response.text
