from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from test_ports_contract import RunRepositoryContract

from agent_api.ports import RunRepository
from agent_api.storage import SQLiteRunRepository, migrate


class TestSQLiteRunRepository(RunRepositoryContract):
    """Execute API-00's reusable suite unchanged against separate SQLite writes."""

    @pytest_asyncio.fixture
    async def repository(self, tmp_path: Path) -> AsyncIterator[RunRepository]:
        path = tmp_path / "contract.sqlite3"
        await migrate(path)
        yield SQLiteRunRepository(path)
