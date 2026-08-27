from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import AnyHttpUrl

from agent_api.ports import (
    ClaimRequest,
    EnqueueResult,
    RunFailureCode,
    RunState,
    StateUpdate,
    WorkItem,
)
from agent_api.services import RunService
from agent_api.storage import (
    SessionRecord,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    SQLiteWorkQueue,
    StorageError,
    TenantRecord,
    migrate,
)
from search_agent import Citation, ScopedAnswer

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class FailOnceQueue:
    def __init__(self, delegate: SQLiteWorkQueue) -> None:
        self._delegate = delegate
        self._failed = False

    async def enqueue(self, item: WorkItem) -> EnqueueResult:
        if not self._failed:
            self._failed = True
            raise StorageError("simulated queue outage")
        return await self._delegate.enqueue(item)

    async def cancel(self, *, tenant_id: str, run_id: str) -> int:
        return await self._delegate.cancel(tenant_id=tenant_id, run_id=run_id)


async def _service_storage(path: Path) -> tuple[SQLiteRunRepository, SQLiteWorkQueue]:
    await migrate(path)
    await SQLiteTenantRepository(path).put(
        TenantRecord(tenant_id="tenant-one", created_at=NOW)
    )
    await SQLiteSessionRepository(path).put(
        SessionRecord(
            tenant_id="tenant-one",
            session_id="session-one",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return SQLiteRunRepository(path), SQLiteWorkQueue(path)


@pytest.mark.asyncio
async def test_idempotent_retry_repairs_a_failed_enqueue(tmp_path: Path) -> None:
    repository, queue = await _service_storage(tmp_path / "retry.sqlite3")
    generated_ids = iter(("run-original", "run-discarded"))
    service = RunService(
        repository,
        FailOnceQueue(queue),
        clock=lambda: NOW,
        run_id_factory=lambda: next(generated_ids),
    )

    with pytest.raises(StorageError, match="simulated queue outage"):
        await service.submit(
            tenant_id="tenant-one",
            session_id="session-one",
            idempotency_key="request-key-one",
            query="Find the documented answer.",
        )

    persisted = await repository.get(tenant_id="tenant-one", run_id="run-original")
    assert persisted is not None and persisted.state is RunState.QUEUED

    retried = await service.submit(
        tenant_id="tenant-one",
        session_id="session-one",
        idempotency_key="request-key-one",
        query="Find the documented answer.",
    )
    work = await queue.receive(now=NOW, visibility_seconds=30)

    assert retried.run_id == "run-original"
    assert work is not None and work.run_id == "run-original"


@pytest.mark.asyncio
async def test_terminal_idempotent_retry_returns_the_original_acceptance(
    tmp_path: Path,
) -> None:
    repository, queue = await _service_storage(tmp_path / "terminal.sqlite3")
    service = RunService(
        repository,
        queue,
        clock=lambda: NOW,
        run_id_factory=lambda: "run-original",
    )
    first = await service.submit(
        tenant_id="tenant-one",
        session_id="session-one",
        idempotency_key="request-key-one",
        query="Find the documented answer.",
    )
    claimed = await repository.claim(
        ClaimRequest(
            tenant_id="tenant-one",
            run_id=first.run_id,
            worker_id="worker-one",
            lease_id="lease-one",
            now=NOW,
            lease_seconds=30,
        )
    )
    assert claimed.run is not None
    await repository.compare_and_set(
        StateUpdate(
            tenant_id="tenant-one",
            run_id=first.run_id,
            expected_version=claimed.run.version,
            expected_state=RunState.RUNNING,
            next_state=RunState.COMPLETED,
            at=NOW + timedelta(seconds=1),
            worker_id="worker-one",
            lease_id="lease-one",
            answer=_answer(),
        )
    )

    retried = await service.submit(
        tenant_id="tenant-one",
        session_id="session-one",
        idempotency_key="request-key-one",
        query="Find the documented answer.",
    )

    assert retried == first
    assert await queue.receive(now=NOW, visibility_seconds=30) is not None
    assert await queue.receive(now=NOW, visibility_seconds=30) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_code", "state", "message", "retryable"),
    [
        (
            RunFailureCode.BUDGET_EXHAUSTED,
            RunState.FAILED,
            "Run exhausted its configured budget.",
            False,
        ),
        (
            RunFailureCode.NO_EVIDENCE,
            RunState.FAILED,
            "No sufficient public evidence was found.",
            False,
        ),
        (
            RunFailureCode.SEARCH_FAILED,
            RunState.FAILED,
            "Public evidence search failed.",
            True,
        ),
        (
            RunFailureCode.VALIDATION_FAILED,
            RunState.FAILED,
            "Run output failed validation.",
            False,
        ),
        (
            RunFailureCode.EXECUTION_FAILED,
            RunState.FAILED,
            "Run execution failed.",
            True,
        ),
        (
            RunFailureCode.EXPIRED,
            RunState.EXPIRED,
            "Run expired before completion.",
            True,
        ),
    ],
)
async def test_status_projects_only_safe_failure_details(
    tmp_path: Path,
    failure_code: RunFailureCode,
    state: RunState,
    message: str,
    retryable: bool,
) -> None:
    repository, queue = await _service_storage(
        tmp_path / f"{failure_code.value}.sqlite3"
    )
    service = RunService(
        repository,
        queue,
        clock=lambda: NOW,
        run_id_factory=lambda: "run-one",
    )
    await service.submit(
        tenant_id="tenant-one",
        session_id="session-one",
        idempotency_key="request-key-one",
        query="Find the documented answer.",
    )
    claimed = await repository.claim(
        ClaimRequest(
            tenant_id="tenant-one",
            run_id="run-one",
            worker_id="worker-one",
            lease_id="lease-one",
            now=NOW,
            lease_seconds=30,
        )
    )
    assert claimed.run is not None
    await repository.compare_and_set(
        StateUpdate(
            tenant_id="tenant-one",
            run_id="run-one",
            expected_version=claimed.run.version,
            expected_state=RunState.RUNNING,
            next_state=state,
            at=NOW + timedelta(seconds=1),
            worker_id="worker-one",
            lease_id="lease-one",
            failure_code=failure_code,
        )
    )

    status = await service.get(tenant_id="tenant-one", run_id="run-one")

    assert status.failure is not None
    assert status.failure.code is failure_code
    assert status.failure.message == message
    assert status.failure.retryable is retryable


def _answer() -> ScopedAnswer:
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
