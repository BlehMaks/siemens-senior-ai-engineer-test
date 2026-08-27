from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_api.services import SessionService, SessionUnavailable
from agent_api.storage import (
    SessionRecord,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    TenantRecord,
    migrate,
)

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_session_id_collisions_retry_with_a_fixed_bound(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    await migrate(path)
    await SQLiteTenantRepository(path).put(
        TenantRecord(tenant_id="tenant-one", created_at=NOW)
    )
    repository = SQLiteSessionRepository(path)
    await repository.put(
        SessionRecord(
            tenant_id="tenant-one",
            session_id="session-existing",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    identifiers = iter(("session-existing", "session-new"))
    service = SessionService(
        repository,
        clock=lambda: NOW,
        id_factory=lambda: next(identifiers),
    )
    created = await service.create(tenant_id="tenant-one", label=None)
    assert created.session_id == "session-new"

    exhausted = SessionService(
        repository,
        clock=lambda: NOW,
        id_factory=lambda: "session-existing",
    )
    with pytest.raises(SessionUnavailable):
        await exhausted.create(tenant_id="tenant-one", label=None)
