from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_api.storage import MigrationError, migrate, schema
from search_agent.memory import SQLiteReflectionRepository


@pytest.mark.asyncio
async def test_empty_and_repeated_migration_are_stable(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"

    await migrate(path)
    with sqlite3.connect(path) as connection:
        first_tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        )
        first_ledger = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations"
        ).fetchall()

    await migrate(path)
    with sqlite3.connect(path) as connection:
        second_tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        )
        second_ledger = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations"
        ).fetchall()

    assert first_tables == second_tables
    assert first_ledger == second_ledger
    assert {
        "api_key_hashes",
        "audit_entries",
        "idempotency_records",
        "run_events",
        "run_reflections",
        "runs",
        "sessions",
        "tenants",
    } < set(first_tables)


@pytest.mark.asyncio
async def test_migration_adopts_an_existing_task1_memory_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "task1.sqlite3"
    SQLiteReflectionRepository(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO run_reflections "
            "(tenant_id, session_id, run_id, payload) VALUES (?, ?, ?, ?)",
            ("tenant-one", "session-one", "run-one", "{}"),
        )

    await migrate(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT tenant_id, session_id, run_id, payload FROM run_reflections"
        ).fetchall() == [("tenant-one", "session-one", "run-one", "{}")]
        assert connection.execute("SELECT tenant_id FROM tenants").fetchall() == [
            ("tenant-one",)
        ]


@pytest.mark.asyncio
async def test_tampered_and_future_migration_history_is_rejected(
    tmp_path: Path,
) -> None:
    tampered = tmp_path / "tampered.sqlite3"
    await migrate(tampered)
    with sqlite3.connect(tampered) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
            ("0" * 64, 1),
        )
    with pytest.raises(MigrationError, match="history"):
        await migrate(tampered)

    future = tmp_path / "future.sqlite3"
    await migrate(future)
    with sqlite3.connect(future) as connection:
        connection.execute(
            "INSERT INTO schema_migrations "
            "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
            (2, "future", "1" * 64, "2026-08-27T10:00:00+00:00"),
        )
    with pytest.raises(MigrationError, match="newer"):
        await migrate(future)


@pytest.mark.asyncio
async def test_migration_ledger_cannot_conceal_physical_schema_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "drift.sqlite3"
    await migrate(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE run_events")

    with pytest.raises(MigrationError, match="physical schema"):
        await migrate(path)


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_schema_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.sqlite3"
    await migrate(path)
    broken = schema._Migration(
        2,
        "broken",
        "CREATE TABLE rollback_probe (value TEXT);\n"
        "INSERT INTO table_that_does_not_exist VALUES (1);\n",
    )
    monkeypatch.setattr(schema, "_MIGRATIONS", (*schema._MIGRATIONS, broken))

    with pytest.raises(MigrationError, match="failed"):
        await migrate(path)

    with sqlite3.connect(path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        probe = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'rollback_probe'"
        ).fetchone()
    assert versions == [(1,)]
    assert probe is None


@pytest.mark.asyncio
async def test_incompatible_path_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(MigrationError, match="regular file"):
        await migrate(directory)
    with pytest.raises(MigrationError, match="parent"):
        await migrate(tmp_path / "missing" / "database.sqlite3")
