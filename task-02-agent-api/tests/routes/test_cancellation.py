from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openapi_contract import build_contract_app
from pydantic import AnyHttpUrl

from agent_api.app import create_app
from agent_api.ports import ClaimRequest, EnqueueResult, RunState, StateUpdate, WorkItem
from agent_api.schemas import RunEventType
from agent_api.services import RunService
from agent_api.storage import (
    SessionRecord,
    SQLiteAuditRepository,
    SQLiteEventRepository,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    SQLiteWorkQueue,
    TenantRecord,
)
from search_agent import Citation, ScopedAnswer

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
CORRELATION_ID = "corr-cancel-client-0001"


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


class FailOnceCleanupQueue:
    def __init__(self, delegate: SQLiteWorkQueue) -> None:
        self._delegate = delegate
        self._failed = False

    async def enqueue(self, item: WorkItem) -> EnqueueResult:
        return await self._delegate.enqueue(item)

    async def cancel(self, *, tenant_id: str, run_id: str) -> int:
        if not self._failed:
            self._failed = True
            raise RuntimeError("queue cleanup unavailable")
        return await self._delegate.cancel(tenant_id=tenant_id, run_id=run_id)


@dataclass(frozen=True)
class CancellationContext:
    app: FastAPI
    client: AsyncClient
    database_path: Path


@pytest_asyncio.fixture
async def cancellation_context(tmp_path: Path) -> AsyncIterator[CancellationContext]:
    database_path = tmp_path / "cancellation.sqlite3"
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
        yield CancellationContext(app=app, client=client, database_path=database_path)


async def tenant_key(
    context: CancellationContext,
    *,
    tenant_id: str,
    scopes: tuple[str, ...],
    create_session: bool = False,
) -> str:
    await SQLiteTenantRepository(context.database_path).put(
        TenantRecord(tenant_id=tenant_id, created_at=NOW)
    )
    if create_session:
        await SQLiteSessionRepository(context.database_path).put(
            SessionRecord(
                tenant_id=tenant_id,
                session_id="session-one",
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


def headers(authorization: str | None) -> dict[str, str]:
    values = {"X-Correlation-ID": CORRELATION_ID}
    if authorization is not None:
        values["Authorization"] = authorization
    return values


async def submit(context: CancellationContext, authorization: str) -> str:
    response = await context.client.post(
        "/v1/sessions/session-one/runs",
        json={"query": "Find the documented public answer."},
        headers={
            **headers(authorization),
            "Idempotency-Key": "request-key-one",
        },
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert type(run_id) is str
    return run_id


def assert_error(payload: object, *, code: str, message: str) -> None:
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
async def test_operational_cancellation_matches_frozen_openapi(
    cancellation_context: CancellationContext,
) -> None:
    path = "/v1/runs/{run_id}/cancel"
    operational = cancellation_context.app.openapi()["paths"][path]
    frozen = build_contract_app().openapi()["paths"][path]

    assert operational == frozen


@pytest.mark.asyncio
async def test_queued_cancellation_is_persisted_idempotent_and_emitted_once(
    cancellation_context: CancellationContext,
) -> None:
    authorization = await tenant_key(
        cancellation_context,
        tenant_id="tenant-one",
        scopes=("runs:read", "runs:write"),
        create_session=True,
    )
    run_id = await submit(cancellation_context, authorization)

    first = await cancellation_context.client.post(
        f"/v1/runs/{run_id}/cancel", headers=headers(authorization)
    )
    repeated = await cancellation_context.client.post(
        f"/v1/runs/{run_id}/cancel", headers=headers(authorization)
    )

    expected = {
        "run_id": run_id,
        "state": "cancelled",
        "cancellation_requested": True,
        "changed": True,
        "requested_at": "2026-08-27T10:00:00Z",
    }
    assert first.status_code == repeated.status_code == 202
    assert first.json() == expected
    assert repeated.json() == {**expected, "changed": False}

    stored = await SQLiteRunRepository(cancellation_context.database_path).get(
        tenant_id="tenant-one", run_id=run_id
    )
    events = await SQLiteEventRepository(cancellation_context.database_path).list(
        tenant_id="tenant-one", run_id=run_id
    )
    work = await SQLiteWorkQueue(cancellation_context.database_path).receive(
        now=NOW, visibility_seconds=30
    )
    assert stored is not None and stored.state is RunState.CANCELLED
    assert stored.cancellation_requested_at == NOW
    assert work is None
    assert [event.event_type for event in events].count(RunEventType.CANCELLED) == 1
    terminal = [
        sample
        for sample in cancellation_context.app.state.telemetry.snapshot()
        if sample.name == "api_runs_terminal_total"
        and dict(sample.labels)["state"] == "cancelled"
    ]
    assert len(terminal) == 1 and terminal[0].value == 1
    audit = await SQLiteAuditRepository(cancellation_context.database_path).list(
        tenant_id="tenant-one"
    )
    assert [entry.action for entry in audit].count("run.cancelled") == 1


@pytest.mark.asyncio
async def test_queued_cancellation_observability_survives_cleanup_retry(
    cancellation_context: CancellationContext,
) -> None:
    authorization = await tenant_key(
        cancellation_context,
        tenant_id="tenant-one",
        scopes=("runs:write",),
        create_session=True,
    )
    run_id = await submit(cancellation_context, authorization)
    durable_queue = SQLiteWorkQueue(cancellation_context.database_path)
    cancellation_context.app.state.run_service = RunService(
        SQLiteRunRepository(cancellation_context.database_path),
        FailOnceCleanupQueue(durable_queue),
        clock=lambda: NOW,
    )

    first = await cancellation_context.client.post(
        f"/v1/runs/{run_id}/cancel", headers=headers(authorization)
    )
    repaired = await cancellation_context.client.post(
        f"/v1/runs/{run_id}/cancel", headers=headers(authorization)
    )

    assert first.status_code == repaired.status_code == 202
    assert first.json()["changed"] is True
    assert repaired.json()["changed"] is False
    assert await durable_queue.receive(now=NOW, visibility_seconds=30) is None
    terminal = [
        sample
        for sample in cancellation_context.app.state.telemetry.snapshot()
        if sample.name == "api_runs_terminal_total"
        and dict(sample.labels)["state"] == "cancelled"
    ]
    assert len(terminal) == 1 and terminal[0].value == 1
    audit = await SQLiteAuditRepository(cancellation_context.database_path).list(
        tenant_id="tenant-one"
    )
    assert [entry.action for entry in audit].count("run.cancelled") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [RunState.RUNNING, RunState.WAITING_FOR_TOOL])
async def test_active_cancellation_persists_request_without_dropping_dispatch(
    cancellation_context: CancellationContext,
    state: RunState,
) -> None:
    authorization = await tenant_key(
        cancellation_context,
        tenant_id="tenant-one",
        scopes=("runs:write",),
        create_session=True,
    )
    run_id = await submit(cancellation_context, authorization)
    repository = SQLiteRunRepository(cancellation_context.database_path)
    claimed = await repository.claim(
        ClaimRequest(
            tenant_id="tenant-one",
            run_id=run_id,
            worker_id="worker-one",
            lease_id="lease-one",
            now=NOW,
            lease_seconds=30,
        )
    )
    assert claimed.run is not None
    active = claimed.run
    if state is RunState.WAITING_FOR_TOOL:
        transition = await repository.compare_and_set(
            StateUpdate(
                tenant_id="tenant-one",
                run_id=run_id,
                expected_version=active.version,
                expected_state=RunState.RUNNING,
                next_state=RunState.WAITING_FOR_TOOL,
                at=NOW,
                worker_id="worker-one",
                lease_id="lease-one",
            )
        )
        assert transition.run is not None

    response = await cancellation_context.client.post(
        f"/v1/runs/{run_id}/cancel", headers=headers(authorization)
    )

    assert response.status_code == 202
    assert response.json() == {
        "run_id": run_id,
        "state": state.value,
        "cancellation_requested": True,
        "changed": True,
        "requested_at": "2026-08-27T10:00:00Z",
    }
    stored = await repository.get(tenant_id="tenant-one", run_id=run_id)
    events = await SQLiteEventRepository(cancellation_context.database_path).list(
        tenant_id="tenant-one", run_id=run_id
    )
    work = await SQLiteWorkQueue(cancellation_context.database_path).receive(
        now=NOW, visibility_seconds=30
    )
    assert stored is not None and stored.state is state
    assert stored.cancellation_requested_at == NOW
    assert work is not None and work.run_id == run_id
    assert all(event.event_type is not RunEventType.CANCELLED for event in events)


@pytest.mark.asyncio
async def test_completed_run_never_regresses_to_cancelled(
    cancellation_context: CancellationContext,
) -> None:
    authorization = await tenant_key(
        cancellation_context,
        tenant_id="tenant-one",
        scopes=("runs:write",),
        create_session=True,
    )
    run_id = await submit(cancellation_context, authorization)
    repository = SQLiteRunRepository(cancellation_context.database_path)
    claimed = await repository.claim(
        ClaimRequest(
            tenant_id="tenant-one",
            run_id=run_id,
            worker_id="worker-one",
            lease_id="lease-one",
            now=NOW,
            lease_seconds=30,
        )
    )
    assert claimed.run is not None
    completed_at = NOW + timedelta(seconds=1)
    write = await repository.compare_and_set(
        StateUpdate(
            tenant_id="tenant-one",
            run_id=run_id,
            expected_version=claimed.run.version,
            expected_state=RunState.RUNNING,
            next_state=RunState.COMPLETED,
            at=completed_at,
            worker_id="worker-one",
            lease_id="lease-one",
            answer=answer(),
        )
    )
    assert write.run is not None and write.run.state is RunState.COMPLETED

    response = await cancellation_context.client.post(
        f"/v1/runs/{run_id}/cancel", headers=headers(authorization)
    )

    assert response.status_code == 202
    assert response.json() == {
        "run_id": run_id,
        "state": "completed",
        "cancellation_requested": False,
        "changed": False,
        "requested_at": None,
    }
    stored = await repository.get(tenant_id="tenant-one", run_id=run_id)
    events = await SQLiteEventRepository(cancellation_context.database_path).list(
        tenant_id="tenant-one", run_id=run_id
    )
    assert stored is not None and stored.state is RunState.COMPLETED
    assert [event.event_type for event in events].count(RunEventType.COMPLETED) == 1
    assert all(event.event_type is not RunEventType.CANCELLED for event in events)


@pytest.mark.asyncio
async def test_cancellation_authenticates_before_path_validation_and_hides_tenants(
    cancellation_context: CancellationContext,
) -> None:
    owner = await tenant_key(
        cancellation_context,
        tenant_id="tenant-one",
        scopes=("runs:write",),
        create_session=True,
    )
    foreign = await tenant_key(
        cancellation_context,
        tenant_id="tenant-foreign",
        scopes=("runs:write",),
    )
    reader = await tenant_key(
        cancellation_context,
        tenant_id="tenant-reader",
        scopes=("runs:read",),
    )
    run_id = await submit(cancellation_context, owner)

    missing_auth = await cancellation_context.client.post(
        "/v1/runs/not-an-opaque id/cancel", headers=headers(None)
    )
    wrong_scope = await cancellation_context.client.post(
        "/v1/runs/not-an-opaque id/cancel", headers=headers(reader)
    )
    foreign_run = await cancellation_context.client.post(
        f"/v1/runs/{run_id}/cancel", headers=headers(foreign)
    )
    absent_run = await cancellation_context.client.post(
        "/v1/runs/run-absent/cancel", headers=headers(foreign)
    )

    assert missing_auth.status_code == 401
    assert_error(
        missing_auth.json(),
        code="unauthenticated",
        message="Authentication failed.",
    )
    assert wrong_scope.status_code == 403
    assert_error(wrong_scope.json(), code="forbidden", message="Forbidden.")
    assert foreign_run.status_code == absent_run.status_code == 404
    assert foreign_run.json() == absent_run.json()
    assert_error(foreign_run.json(), code="not_found", message="Run was not found.")


def answer() -> ScopedAnswer:
    return ScopedAnswer(
        answer_text="The documented answer is supported by the cited source.",
        citations=(
            Citation(
                claim="The source supports the answer.",
                evidence_id="ev-source",
                source_url=AnyHttpUrl("https://example.com/source"),
            ),
        ),
    )
