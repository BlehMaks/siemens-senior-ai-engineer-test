"""Forward-only, checksum-protected SQLite schema migration runner."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import aiosqlite


class MigrationError(RuntimeError):
    """A safe migration failure without database contents or SQL text."""


@dataclass(frozen=True)
class _Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def _bundled_migration(version: int, name: str, filename: str) -> _Migration:
    sql = files("agent_api.migrations").joinpath(filename).read_text(encoding="utf-8")
    return _Migration(version, name, sql)


_MIGRATIONS = (
    _bundled_migration(1, "initial", "001_initial.sql"),
    _bundled_migration(2, "api-key-lifecycle", "002_api_key_lifecycle.sql"),
    _bundled_migration(3, "local-work-queue", "003_local_work_queue.sql"),
    _bundled_migration(4, "quota-accounting", "004_quota_accounting.sql"),
    _bundled_migration(5, "work-item-generation", "005_work_item_generation.sql"),
    _bundled_migration(6, "semantic-facts", "006_semantic_facts.sql"),
)
_CREATE_LEDGER = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
"""
_REQUIRED_COLUMNS = {
    "schema_migrations": ("version", "name", "checksum", "applied_at"),
    "tenants": ("tenant_id", "display_name", "created_at"),
    "api_key_hashes": (
        "tenant_id",
        "key_id",
        "key_hash",
        "created_at",
        "expires_at",
        "revoked_at",
        "scopes",
        "rotated_from_key_id",
    ),
    "sessions": ("tenant_id", "session_id", "label", "created_at", "updated_at"),
    "runs": (
        "tenant_id",
        "run_id",
        "session_id",
        "state",
        "version",
        "created_at",
        "payload",
    ),
    "idempotency_records": (
        "tenant_id",
        "idempotency_key",
        "request_hash",
        "run_id",
        "created_at",
    ),
    "run_events": ("tenant_id", "run_id", "sequence", "occurred_at", "payload"),
    "run_reflections": ("tenant_id", "session_id", "run_id", "payload"),
    "semantic_facts": (
        "tenant_id",
        "fact_id",
        "origin_session_id",
        "origin_run_id",
        "source_id",
        "conflict_key",
        "state",
        "expires_at",
        "payload",
    ),
    "audit_entries": ("tenant_id", "entry_id", "action", "occurred_at"),
    "work_items": (
        "work_id",
        "tenant_id",
        "run_id",
        "enqueued_at",
        "not_before",
        "generation_id",
    ),
    "quota_rate_buckets": ("tenant_id", "key_id", "tokens", "last_refill"),
    "quota_run_admissions": (
        "tenant_id",
        "key_id",
        "idempotency_key",
        "request_hash",
        "run_id",
        "work_day",
        "work_units",
        "created_at",
    ),
    "quota_execution_leases": (
        "tenant_id",
        "run_id",
        "permit_id",
        "expires_at",
    ),
    "quota_sse_leases": ("tenant_id", "key_id", "permit_id", "expires_at"),
}
_LEGACY_REFLECTION_SQL = (
    "CREATE TABLE run_reflections ( tenant_id TEXT NOT NULL, "
    "session_id TEXT NOT NULL, run_id TEXT NOT NULL, "
    "payload TEXT NOT NULL CHECK(length(payload) <= 65536), "
    "PRIMARY KEY (tenant_id, session_id, run_id) ) WITHOUT ROWID"
)
_LEGACY_SEMANTIC_SQL = (
    "CREATE TABLE semantic_facts ( tenant_id TEXT NOT NULL, "
    "fact_id TEXT NOT NULL, origin_session_id TEXT NOT NULL, "
    "origin_run_id TEXT NOT NULL, source_id TEXT NOT NULL, "
    "conflict_key TEXT NOT NULL, "
    "state TEXT NOT NULL, expires_at TEXT NOT NULL, "
    "payload TEXT NOT NULL CHECK(length(payload) <= 16384), "
    "PRIMARY KEY (tenant_id, fact_id) ) WITHOUT ROWID"
)


async def migrate(path: Path) -> None:
    """Bring a regular SQLite file to the latest known schema exactly once."""

    _validate_path(path)
    try:
        async with aiosqlite.connect(path, isolation_level=None) as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(_CREATE_LEDGER)
                rows = tuple(
                    await (
                        await connection.execute(
                            "SELECT version, name, checksum FROM schema_migrations "
                            "ORDER BY version"
                        )
                    ).fetchall()
                )
                _validate_ledger(rows)
                if not rows:
                    await _validate_legacy_memory_schema(connection)
                applied = len(rows)
                for migration in _MIGRATIONS[applied:]:
                    for statement in _statements(migration.sql):
                        await connection.execute(statement)
                    await connection.execute(
                        "INSERT INTO schema_migrations "
                        "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (
                            migration.version,
                            migration.name,
                            migration.checksum,
                            datetime.now(UTC).isoformat(timespec="microseconds"),
                        ),
                    )
                await validate_current_schema(connection)
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
    except MigrationError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise MigrationError("SQLite migration failed") from exc


def _validate_path(path: Path) -> None:
    if not isinstance(path, Path):
        raise MigrationError("SQLite path must be a filesystem path")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise MigrationError("SQLite path must be a regular file")
    if not path.parent.is_dir():
        raise MigrationError("SQLite parent directory does not exist")


def _validate_ledger(rows: Sequence[Sequence[object]]) -> None:
    if len(rows) > len(_MIGRATIONS):
        raise MigrationError("database schema is newer than this application")
    for index, row in enumerate(rows):
        expected = _MIGRATIONS[index]
        if tuple(row) != (expected.version, expected.name, expected.checksum):
            raise MigrationError("database migration history is incompatible")


async def validate_current_schema(connection: aiosqlite.Connection) -> None:
    """Verify that a read-only connection matches this build's complete schema."""

    rows = tuple(
        await (
            await connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            )
        ).fetchall()
    )
    _validate_ledger(rows)
    if len(rows) != len(_MIGRATIONS):
        raise MigrationError("database schema is not current")
    await _validate_physical_schema(connection)


def _statements(script: str) -> tuple[str, ...]:
    """Split bundled SQL with SQLite's own completeness parser."""

    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(pending.strip())
            pending = ""
    if pending.strip():
        raise MigrationError("bundled migration is incomplete")
    return tuple(statements)


async def _validate_legacy_reflection_schema(
    connection: aiosqlite.Connection,
) -> None:
    """Accept only the Task 1 table shape before migration takes ownership."""

    existing = await (
        await connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'run_reflections' COLLATE NOCASE"
        )
    ).fetchone()
    if existing is None:
        return
    name, sql = existing
    if (
        name != "run_reflections"
        or type(sql) is not str
        or " ".join(sql.split()) != _LEGACY_REFLECTION_SQL
    ):
        raise MigrationError("database reflection schema is incompatible")
    extra_objects = await (
        await connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE tbl_name = 'run_reflections' COLLATE NOCASE "
            "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
        )
    ).fetchall()
    if extra_objects:
        raise MigrationError("database reflection schema is incompatible")


async def _validate_legacy_memory_schema(
    connection: aiosqlite.Connection,
) -> None:
    await _validate_legacy_reflection_schema(connection)
    await _validate_legacy_table(
        connection,
        table="semantic_facts",
        expected_sql=_LEGACY_SEMANTIC_SQL,
        error="database semantic fact schema is incompatible",
    )


async def _validate_legacy_table(
    connection: aiosqlite.Connection,
    *,
    table: str,
    expected_sql: str,
    error: str,
) -> None:
    existing = await (
        await connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name = ? COLLATE NOCASE",
            (table,),
        )
    ).fetchone()
    if existing is None:
        return
    name, sql = existing
    if name != table or type(sql) is not str or " ".join(sql.split()) != expected_sql:
        raise MigrationError(error)
    extra_objects = await (
        await connection.execute(
            "SELECT 1 FROM sqlite_master WHERE tbl_name = ? COLLATE NOCASE "
            "AND type IN ('index', 'trigger') AND sql IS NOT NULL",
            (table,),
        )
    ).fetchall()
    if extra_objects:
        raise MigrationError(error)


async def _validate_physical_schema(connection: aiosqlite.Connection) -> None:
    """Ensure the checksum ledger still describes the database it guards."""

    tables = {
        row[0]
        for row in await (
            await connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ).fetchall()
    }
    if not set(_REQUIRED_COLUMNS) <= tables:
        raise MigrationError("database physical schema is incompatible")
    for table, required in _REQUIRED_COLUMNS.items():
        rows = await (
            await connection.execute(f'PRAGMA table_info("{table}")')
        ).fetchall()
        columns = {row[1] for row in rows}
        if not set(required) <= columns:
            raise MigrationError("database physical schema is incompatible")

    reflection_foreign_keys = await (
        await connection.execute('PRAGMA foreign_key_list("run_reflections")')
    ).fetchall()
    if not any(
        row[2] == "tenants"
        and row[3] == "tenant_id"
        and row[4] == "tenant_id"
        and row[6].upper() == "CASCADE"
        for row in reflection_foreign_keys
    ):
        raise MigrationError("database reflection schema is incompatible")

    semantic_foreign_keys = await (
        await connection.execute('PRAGMA foreign_key_list("semantic_facts")')
    ).fetchall()
    semantic_session_reference = {
        (row[3], row[4])
        for row in semantic_foreign_keys
        if row[2] == "sessions" and row[6].upper() == "CASCADE"
    }
    if semantic_session_reference != {
        ("tenant_id", "tenant_id"),
        ("origin_session_id", "session_id"),
    }:
        raise MigrationError("database semantic fact schema is incompatible")

    queue_foreign_keys = await (
        await connection.execute('PRAGMA foreign_key_list("work_items")')
    ).fetchall()
    run_reference = {
        (row[3], row[4])
        for row in queue_foreign_keys
        if row[2] == "runs" and row[6].upper() == "CASCADE"
    }
    if run_reference != {("tenant_id", "tenant_id"), ("run_id", "run_id")}:
        raise MigrationError("database physical schema is incompatible")

    queue_indexes = {
        "work_items_by_due": ("not_before", "enqueued_at", "work_id"),
        "work_items_by_run": ("tenant_id", "run_id", "work_id"),
    }
    for name, expected_columns in queue_indexes.items():
        index_columns = tuple(
            row[2]
            for row in await (
                await connection.execute(f'PRAGMA index_info("{name}")')
            ).fetchall()
        )
        if index_columns != expected_columns:
            raise MigrationError("database physical schema is incompatible")

    work_columns = tuple(
        row[1]
        for row in await (
            await connection.execute('PRAGMA table_info("work_items")')
        ).fetchall()
    )
    if work_columns != _REQUIRED_COLUMNS["work_items"]:
        raise MigrationError("database physical schema is incompatible")


__all__ = ["MigrationError", "migrate", "validate_current_schema"]
