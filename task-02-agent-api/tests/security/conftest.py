from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from agent_api.storage import migrate


@pytest_asyncio.fixture
async def migrated_path(tmp_path: Path) -> Path:
    path = tmp_path / "security.sqlite3"
    await migrate(path)
    return path
