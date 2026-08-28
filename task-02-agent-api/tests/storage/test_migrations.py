from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_api.storage import MigrationError, migrate, schema
from search_agent.memory import (
    FactAuthor,
    ProcedureAuthor,
    ProcedureReviewState,
    ProcedureVersion,
    SemanticFact,
    SQLiteProcedureRepository,
    SQLiteReflectionRepository,
    SQLiteSemanticFactRepository,
)


def install_api_schema_through_v6(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(schema._CREATE_LEDGER)
        for migration in schema._MIGRATIONS[:6]:
            connection.executescript(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations "
                "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    "2026-08-28T10:00:00.000000+00:00",
                ),
            )


def install_api_schema_through_v7(path: Path) -> None:
    install_api_schema_through_v6(path)
    migration = schema._MIGRATIONS[6]
    with sqlite3.connect(path) as connection:
        connection.executescript(migration.sql)
        connection.execute(
            "INSERT INTO schema_migrations "
            "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
            (
                migration.version,
                migration.name,
                migration.checksum,
                "2026-08-28T10:00:00.000000+00:00",
            ),
        )


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
        "active_procedures",
        "audit_entries",
        "idempotency_records",
        "run_events",
        "run_reflections",
        "semantic_facts",
        "runs",
        "sessions",
        "tenants",
        "work_items",
        "quota_execution_leases",
        "quota_rate_buckets",
        "quota_run_admissions",
        "quota_sse_leases",
        "procedure_versions",
        "procedure_version_heads",
    } < set(first_tables)


@pytest.mark.asyncio
async def test_migration_adopts_an_existing_task1_memory_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "task1.sqlite3"
    SQLiteReflectionRepository(path).close()
    semantic = SQLiteSemanticFactRepository(path)
    semantic.propose(
        SemanticFact(
            tenant_id="tenant-one",
            fact_id="fact-one",
            origin_session_id="session-one",
            origin_run_id="run-one",
            claim="Siemens reports scope three emissions.",
            conflict_key="siemens-scope-three",
            source_id="source-one",
            evidence_id="ev-report",
            source_url="https://www.siemens.com/reports/sustainability-2025",
            proposed_at=datetime(2026, 8, 28, tzinfo=UTC),
            expires_at=datetime(2027, 8, 28, tzinfo=UTC),
            author=FactAuthor.HUMAN,
        )
    )
    semantic.close()
    procedures = SQLiteProcedureRepository(path)
    procedures.propose(
        ProcedureVersion(
            tenant_id="tenant-one",
            procedure_id="playbook-one",
            version=1,
            origin_session_id="session-one",
            origin_run_id="run-one",
            title="Review sustainability evidence",
            steps=("Prefer the official issuer report.",),
            proposed_at=datetime(2026, 8, 28, tzinfo=UTC),
            author=ProcedureAuthor.HUMAN,
        ),
        expected_latest_version=None,
    )
    procedures.review(
        tenant_id="tenant-one",
        procedure_id="playbook-one",
        version=1,
        state=ProcedureReviewState.APPROVED,
        reviewer_id="reviewer-one",
        reviewed_at=datetime(2026, 8, 28, 1, tzinfo=UTC),
    )
    procedures.activate(
        tenant_id="tenant-one",
        procedure_id="playbook-one",
        version=1,
        expected_active_version=None,
    )
    procedures.close()
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
        assert connection.execute(
            "SELECT tenant_id, fact_id FROM semantic_facts"
        ).fetchall() == [("tenant-one", "fact-one")]
        assert connection.execute(
            "SELECT tenant_id, procedure_id, version FROM procedure_versions"
        ).fetchall() == [("tenant-one", "playbook-one", 1)]
        assert connection.execute(
            "SELECT tenant_id, procedure_id, version FROM active_procedures"
        ).fetchall() == [("tenant-one", "playbook-one", 1)]
        assert connection.execute(
            "SELECT tenant_id, procedure_id, latest_version "
            "FROM procedure_version_heads"
        ).fetchall() == [("tenant-one", "playbook-one", 1)]


@pytest.mark.asyncio
async def test_v8_does_not_restore_a_tenant_deleted_on_v7(tmp_path: Path) -> None:
    path = tmp_path / "v7-deleted-tenant-with-head.sqlite3"
    install_api_schema_through_v7(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO tenants (tenant_id, created_at) VALUES (?, ?)",
            ("tenant-one", "2026-08-28T10:00:00.000000+00:00"),
        )
        connection.execute(
            "INSERT INTO sessions "
            "(tenant_id, session_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (
                "tenant-one",
                "session-one",
                "2026-08-28T10:00:00.000000+00:00",
                "2026-08-28T10:00:00.000000+00:00",
            ),
        )
        connection.execute(SQLiteProcedureRepository._CREATE_HEADS)
        connection.execute(
            "INSERT INTO procedure_version_heads "
            "(tenant_id, procedure_id, latest_version) VALUES (?, ?, ?)",
            ("tenant-one", "playbook-one", 1),
        )
        connection.execute("DELETE FROM tenants WHERE tenant_id = ?", ("tenant-one",))

    await migrate(path)

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = ?", ("tenant-one",)
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM procedure_version_heads WHERE tenant_id = ?",
                ("tenant-one",),
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_id", "latest_version"),
    (("missing-tenant", 1), ("tenant-one", 1.5)),
)
async def test_current_schema_rejects_invalid_procedure_version_heads(
    tmp_path: Path, tenant_id: str, latest_version: int | float
) -> None:
    path = tmp_path / f"invalid-head-{tenant_id}.sqlite3"
    await migrate(path)
    with sqlite3.connect(path) as connection:
        if tenant_id == "tenant-one":
            connection.execute(
                "INSERT INTO tenants (tenant_id, created_at) VALUES (?, ?)",
                (tenant_id, "2026-08-28T10:00:00.000000+00:00"),
            )
        else:
            connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO procedure_version_heads "
            "(tenant_id, procedure_id, latest_version) VALUES (?, ?, ?)",
            (tenant_id, "playbook-one", latest_version),
        )

    with pytest.raises(
        MigrationError, match="procedure version head schema is incompatible"
    ):
        await migrate(path)


@pytest.mark.asyncio
async def test_migration_rejects_an_incomplete_legacy_procedure_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "incomplete-procedures.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE procedure_versions ("
            "tenant_id TEXT NOT NULL, procedure_id TEXT NOT NULL, "
            "version INTEGER NOT NULL, origin_session_id TEXT NOT NULL, "
            "origin_run_id TEXT NOT NULL, state TEXT NOT NULL, "
            "payload TEXT NOT NULL CHECK(length(payload) <= 16384), "
            "PRIMARY KEY (tenant_id, procedure_id, version)) WITHOUT ROWID"
        )

    with pytest.raises(MigrationError, match="procedure schema is incompatible"):
        await migrate(path)


@pytest.mark.asyncio
async def test_v7_preflight_rejects_extended_procedure_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v6-with-extended-procedures.sqlite3"
    install_api_schema_through_v6(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE procedure_versions ("
            "tenant_id TEXT NOT NULL, procedure_id TEXT NOT NULL, "
            "version INTEGER NOT NULL, origin_session_id TEXT NOT NULL, "
            "origin_run_id TEXT NOT NULL, state TEXT NOT NULL, "
            "payload TEXT NOT NULL CHECK(length(payload) <= 16384), "
            "private_metadata TEXT NOT NULL, "
            "PRIMARY KEY (tenant_id, procedure_id, version)) WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TABLE active_procedures ("
            "tenant_id TEXT NOT NULL, procedure_id TEXT NOT NULL, "
            "version INTEGER NOT NULL, "
            "PRIMARY KEY (tenant_id, procedure_id)) WITHOUT ROWID"
        )

    with pytest.raises(MigrationError, match="procedure schema is incompatible"):
        await migrate(path)


@pytest.mark.asyncio
async def test_current_schema_rejects_extra_procedure_columns(tmp_path: Path) -> None:
    path = tmp_path / "current-with-extra-procedure-column.sqlite3"
    await migrate(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "ALTER TABLE procedure_versions ADD COLUMN private_metadata TEXT"
        )

    with pytest.raises(MigrationError, match="procedure schema is incompatible"):
        await migrate(path)


@pytest.mark.asyncio
async def test_current_schema_rejects_dangling_active_procedure_pointer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "current-with-dangling-active.sqlite3"
    await migrate(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO active_procedures "
            "(tenant_id, procedure_id, version) VALUES (?, ?, ?)",
            ("tenant-one", "procedure-one", 1),
        )

    with pytest.raises(MigrationError, match="active procedure schema is incompatible"):
        await migrate(path)


@pytest.mark.asyncio
async def test_current_schema_rejects_split_active_pointer_foreign_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "current-with-split-active-foreign-keys.sqlite3"
    await migrate(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE active_procedures")
        connection.execute(
            "CREATE TABLE active_procedures ("
            "tenant_id TEXT NOT NULL, procedure_id TEXT NOT NULL, "
            "version INTEGER NOT NULL, "
            "PRIMARY KEY (tenant_id, procedure_id), "
            "FOREIGN KEY (tenant_id) REFERENCES procedure_versions(tenant_id) "
            "ON DELETE CASCADE, "
            "FOREIGN KEY (procedure_id) "
            "REFERENCES procedure_versions(procedure_id) ON DELETE CASCADE, "
            "FOREIGN KEY (version) REFERENCES procedure_versions(version) "
            "ON DELETE CASCADE) WITHOUT ROWID"
        )

    with pytest.raises(MigrationError, match="active procedure schema is incompatible"):
        await migrate(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ("missing_primary_key", "update_cascade"))
async def test_current_schema_rejects_active_pointer_constraint_drift(
    tmp_path: Path, drift: str
) -> None:
    path = tmp_path / f"active-pointer-{drift}.sqlite3"
    await migrate(path)
    primary_key = (
        ""
        if drift == "missing_primary_key"
        else ", PRIMARY KEY (tenant_id, procedure_id)"
    )
    update_action = " ON UPDATE CASCADE" if drift == "update_cascade" else ""
    table_suffix = "" if drift == "missing_primary_key" else " WITHOUT ROWID"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE active_procedures")
        connection.execute(
            "CREATE TABLE active_procedures ("
            "tenant_id TEXT NOT NULL, procedure_id TEXT NOT NULL, "
            "version INTEGER NOT NULL"
            f"{primary_key}, FOREIGN KEY (tenant_id, procedure_id, version) "
            "REFERENCES procedure_versions(tenant_id, procedure_id, version)"
            f"{update_action} ON DELETE CASCADE){table_suffix}"
        )

    with pytest.raises(MigrationError, match="active procedure schema is incompatible"):
        await migrate(path)


@pytest.mark.asyncio
async def test_migration_rejects_active_pointer_to_unreviewed_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active-proposed-payload.sqlite3"
    repository = SQLiteProcedureRepository(path)
    repository.propose(
        ProcedureVersion(
            tenant_id="tenant-one",
            procedure_id="playbook-one",
            version=1,
            origin_session_id="session-one",
            origin_run_id="run-one",
            title="Review official evidence",
            steps=("Prefer the issuer's official report.",),
            proposed_at=datetime(2026, 8, 28, tzinfo=UTC),
            author=ProcedureAuthor.HUMAN,
        ),
        expected_latest_version=None,
    )
    repository.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE procedure_versions SET state = 'approved' "
            "WHERE tenant_id = ? AND procedure_id = ?",
            ("tenant-one", "playbook-one"),
        )
        connection.execute(
            "INSERT INTO active_procedures "
            "(tenant_id, procedure_id, version) VALUES (?, ?, ?)",
            ("tenant-one", "playbook-one", 1),
        )

    with pytest.raises(MigrationError, match="active procedure schema is incompatible"):
        await migrate(path)


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
            (9, "future", "1" * 64, "2026-08-27T10:00:00+00:00"),
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
        9,
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
    assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]
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
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]
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

    assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]
    assert "work_items_by_due" in indexes
    assert columns == (
        "work_id",
        "tenant_id",
        "run_id",
        "enqueued_at",
        "not_before",
        "generation_id",
    )


@pytest.mark.asyncio
async def test_generation_migration_binds_existing_work_to_its_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "generation.sqlite3"
    migrations = schema._MIGRATIONS
    with sqlite3.connect(path) as connection:
        connection.execute(schema._CREATE_LEDGER)
        for migration in migrations[:4]:
            connection.executescript(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations "
                "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    "2026-08-27T10:00:00+00:00",
                ),
            )
        connection.execute(
            "INSERT INTO tenants (tenant_id, created_at) VALUES (?, ?)",
            ("tenant-one", "2026-08-27T10:00:00.000000+00:00"),
        )
        connection.execute(
            "INSERT INTO sessions "
            "(tenant_id, session_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (
                "tenant-one",
                "session-one",
                "2026-08-27T10:00:00.000000+00:00",
                "2026-08-27T10:00:00.000000+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO runs "
            "(tenant_id, run_id, session_id, state, version, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "tenant-one",
                "run-one",
                "session-one",
                "queued",
                "0",
                "2026-08-27T10:00:00.000000+00:00",
                '{"tenant_id":"tenant-one","run_id":"run-one"}',
            ),
        )
        connection.execute(
            "INSERT INTO work_items "
            "(work_id, tenant_id, run_id, enqueued_at, not_before) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "work-one",
                "tenant-one",
                "run-one",
                "2026-08-27T10:00:00.000000+00:00",
                "2026-08-27T10:00:00.000000+00:00",
            ),
        )
    await migrate(path)

    with sqlite3.connect(path) as connection:
        run_generation = connection.execute(
            "SELECT json_extract(payload, '$.generation_id') FROM runs"
        ).fetchone()[0]
        work_generation = connection.execute(
            "SELECT generation_id FROM work_items"
        ).fetchone()[0]
    assert run_generation.startswith("generation-")
    assert work_generation == run_generation


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
