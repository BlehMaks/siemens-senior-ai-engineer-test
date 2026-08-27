from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from test_ports_contract import NOW, WorkQueueContract, submission

from agent_api.ports import WorkQueue
from agent_api.storage import (
    SessionRecord,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    SQLiteWorkQueue,
    TenantRecord,
    migrate,
)


class TestSQLiteWorkQueue(WorkQueueContract):
    @pytest_asyncio.fixture
    async def queue(self, tmp_path: Path) -> AsyncIterator[WorkQueue]:
        path = tmp_path / "queue-contract.sqlite3"
        await migrate(path)
        sessions = SQLiteSessionRepository(path)
        for tenant_id in ("tenant-one", "tenant-two"):
            await SQLiteTenantRepository(path).put(
                TenantRecord(tenant_id=tenant_id, created_at=NOW)
            )
            await sessions.put(
                SessionRecord(
                    tenant_id=tenant_id,
                    session_id="session-one",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        runs = SQLiteRunRepository(path)
        await runs.create(submission())
        await runs.create(
            submission(
                tenant_id="tenant-two",
                run_id="run-one",
                idempotency_key="request-key-two",
            )
        )
        yield SQLiteWorkQueue(path)
