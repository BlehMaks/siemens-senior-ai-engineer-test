from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_api.ports import EnqueueResult, RunState, WorkItem
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

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class FailFirstCancellationQueue:
    def __init__(self, delegate: SQLiteWorkQueue) -> None:
        self._delegate = delegate
        self._failed = False

    async def enqueue(self, item: WorkItem) -> EnqueueResult:
        return await self._delegate.enqueue(item)

    async def cancel(self, *, tenant_id: str, run_id: str) -> int:
        if not self._failed:
            self._failed = True
            raise StorageError("private queue cancellation failure")
        return await self._delegate.cancel(tenant_id=tenant_id, run_id=run_id)


@pytest.mark.asyncio
async def test_retry_repairs_queue_after_persisted_cancellation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cancel-repair.sqlite3"
    await migrate(database_path)
    await SQLiteTenantRepository(database_path).put(
        TenantRecord(tenant_id="tenant-one", created_at=NOW)
    )
    await SQLiteSessionRepository(database_path).put(
        SessionRecord(
            tenant_id="tenant-one",
            session_id="session-one",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    repository = SQLiteRunRepository(database_path)
    queue = SQLiteWorkQueue(database_path)
    service = RunService(
        repository,
        FailFirstCancellationQueue(queue),
        clock=lambda: NOW,
        run_id_factory=lambda: "run-one",
    )
    await service.submit(
        tenant_id="tenant-one",
        session_id="session-one",
        idempotency_key="request-key-one",
        query="Find the documented answer.",
    )

    cancelled = await service.cancel(tenant_id="tenant-one", run_id="run-one")
    persisted = await repository.get(tenant_id="tenant-one", run_id="run-one")
    assert persisted is not None and persisted.state is RunState.CANCELLED
    assert cancelled.state is RunState.CANCELLED
    assert cancelled.changed is True

    repaired = await service.cancel(tenant_id="tenant-one", run_id="run-one")

    assert repaired.state is RunState.CANCELLED
    assert repaired.changed is False
    assert repaired.requested_at == NOW
    assert await queue.receive(now=NOW, visibility_seconds=30) is None
