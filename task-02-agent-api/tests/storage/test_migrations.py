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
        "work_items",
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
async def test_migration_rejects_an_incompatible_legacy_reflection_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "incompatible-reflections.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE run_reflections ("
            "tenant_id TEXT, session_id TEXT, run_id TEXT, payload BLOB)"
        )

    with pytest.raises(MigrationError, match="reflection schema is incompatible"):
        await migrate(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("table_name", ["RUN_REFLECTIONS", "run_reflections"])
async def test_migration_preserves_unknown_reflection_table_variants(
    tmp_path: Path, table_name: str
) -> None:
    path = tmp_path / f"{table_name}.sqlite3"
    generated = (
        ", private_metadata TEXT GENERATED ALWAYS AS (payload) VIRTUAL"
        if table_name == "run_reflections"
        else ", private_metadata TEXT NOT NULL"
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"CREATE TABLE {table_name} ("
            "tenant_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "run_id TEXT NOT NULL, payload TEXT NOT NULL"
            f"{generated}, PRIMARY KEY (tenant_id, session_id, run_id)) WITHOUT ROWID"
        )

    with pytest.raises(MigrationError, match="reflection schema is incompatible"):
        await migrate(path)

    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                f'PRAGMA table_xinfo("{table_name}")'
            ).fetchall()
        }
        ledger = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'"
        ).fetchone()
    assert "private_metadata" in columns
    assert ledger is None


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_object", ["constraint", "index", "trigger"])
async def test_migration_rejects_unknown_legacy_schema_objects(
    tmp_path: Path, extra_object: str
) -> None:
    path = tmp_path / f"unknown-{extra_object}.sqlite3"
    unique = ", UNIQUE(payload)" if extra_object == "constraint" else ""
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE run_reflections ("
            "tenant_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "run_id TEXT NOT NULL, "
            "payload TEXT NOT NULL CHECK(length(payload) <= 65536), "
            "PRIMARY KEY (tenant_id, session_id, run_id)"
            f"{unique}) WITHOUT ROWID"
        )
        if extra_object == "index":
            connection.execute(
                "CREATE UNIQUE INDEX unexpected_reflection_payload "
                "ON run_reflections(payload)"
            )
        elif extra_object == "trigger":
            connection.execute(
                "CREATE TRIGGER unexpected_reflection_insert "
                "BEFORE INSERT ON run_reflections BEGIN SELECT 1; END"
            )

    with pytest.raises(MigrationError, match="reflection schema is incompatible"):
        await migrate(path)

    with sqlite3.connect(path) as connection:
        ledger = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'"
        ).fetchone()
        objects = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE tbl_name = 'run_reflections' ORDER BY name"
        ).fetchall()
    assert ledger is None
    assert len(objects) == 2


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
            (4, "future", "1" * 64, "2026-08-27T10:00:00+00:00"),
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
        4,
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
    assert versions == [(1,), (2,), (3,)]
    assert probe is None


@pytest.mark.asyncio
async def test_api_key_lifecycle_migration_keeps_old_rows_non_authorizing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1-keys.sqlite3"
    first = schema._MIGRATIONS[0]
    with sqlite3.connect(path) as connection:
        connection.execute(schema._CREATE_LEDGER)
        connection.executescript(first.sql)
        connection.execute(
            "INSERT INTO schema_migrations "
            "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
            (first.version, first.name, first.checksum, "2026-08-27T10:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO tenants (tenant_id, created_at) VALUES (?, ?)",
            ("tenant-one", "2026-08-27T10:00:00.000000+00:00"),
        )
        connection.execute(
            "INSERT INTO api_key_hashes "
            "(tenant_id, key_id, key_hash, created_at) VALUES (?, ?, ?, ?)",
            (
                "tenant-one",
                "key-one",
                b"h" * 32,
                "2026-08-27T10:00:00.000000+00:00",
            ),
        )

    await migrate(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,)]
        assert connection.execute(
            "SELECT scopes, rotated_from_key_id FROM api_key_hashes "
            "WHERE tenant_id = ? AND key_id = ?",
            ("tenant-one", "key-one"),
        ).fetchone() == ("[]", None)


@pytest.mark.asyncio
async def test_local_work_queue_migration_adds_due_indexes_on_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue.sqlite3"

    await migrate(path)
    await migrate(path)

    with sqlite3.connect(path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        indexes = {
            row[1] for row in connection.execute('PRAGMA index_list("work_items")')
        }
        columns = tuple(
            row[1] for row in connection.execute('PRAGMA table_info("work_items")')
        )

    assert versions == [(1,), (2,), (3,)]
    assert "work_items_by_due" in indexes
    assert columns == (
        "work_id",
        "tenant_id",
        "run_id",
        "enqueued_at",
        "not_before",
    )


@pytest.mark.asyncio
async def test_queue_schema_drift_cannot_hide_behind_the_migration_ledger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue-drift.sqlite3"
    await migrate(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX work_items_by_due")
        connection.execute(
            "CREATE INDEX work_items_by_due ON work_items(enqueued_at, not_before)"
        )

    with pytest.raises(MigrationError, match="physical schema"):
        await migrate(path)


@pytest.mark.asyncio
async def test_incompatible_path_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(MigrationError, match="regular file"):
        await migrate(directory)
    with pytest.raises(MigrationError, match="parent"):
        await migrate(tmp_path / "missing" / "database.sqlite3")
