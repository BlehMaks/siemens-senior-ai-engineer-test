from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from test_ports_contract import NOW, RunRepositoryContract

from agent_api.ports import RunRepository
from agent_api.storage import (
    SessionRecord,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    TenantRecord,
    migrate,
)


class TestSQLiteRunRepository(RunRepositoryContract):
    """Execute API-00's reusable suite unchanged against separate SQLite writes."""

    @pytest_asyncio.fixture
    async def repository(self, tmp_path: Path) -> AsyncIterator[RunRepository]:
        path = tmp_path / "contract.sqlite3"
        await migrate(path)
        for tenant_id in ("tenant-one", "tenant-two"):
            await SQLiteTenantRepository(path).put(
                TenantRecord(tenant_id=tenant_id, created_at=NOW)
            )
        sessions = SQLiteSessionRepository(path)
        for tenant_id, session_id in (
            ("tenant-one", "session-one"),
            ("tenant-one", "session-two"),
            ("tenant-two", "session-one"),
        ):
            await sessions.put(
                SessionRecord(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        yield SQLiteRunRepository(path)
