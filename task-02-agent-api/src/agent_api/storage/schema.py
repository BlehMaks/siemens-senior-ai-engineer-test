"""Forward-only, checksum-protected SQLite schema migration runner."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import aiosqlite
from pydantic import ValidationError

from search_agent.memory import ProcedureReviewState, ProcedureVersion


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
    _bundled_migration(7, "procedure-versions", "007_procedure_versions.sql"),
    _bundled_migration(8, "procedure-version-heads", "008_procedure_version_heads.sql"),
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
    "procedure_versions": (
        "tenant_id",
        "procedure_id",
        "version",
        "origin_session_id",
        "origin_run_id",
        "state",
        "payload",
    ),
    "active_procedures": ("tenant_id", "procedure_id", "version"),
    "procedure_version_heads": (
        "tenant_id",
        "procedure_id",
        "latest_version",
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
_PROCEDURE_LAYOUTS = {
    "procedure_versions": (
        ("tenant_id", "TEXT", 1, 1),
        ("procedure_id", "TEXT", 1, 2),
        ("version", "INTEGER", 1, 3),
        ("origin_session_id", "TEXT", 1, 0),
        ("origin_run_id", "TEXT", 1, 0),
        ("state", "TEXT", 1, 0),
        ("payload", "TEXT", 1, 0),
    ),
    "active_procedures": (
        ("tenant_id", "TEXT", 1, 1),
        ("procedure_id", "TEXT", 1, 2),
        ("version", "INTEGER", 1, 0),
    ),
    "procedure_version_heads": (
        ("tenant_id", "TEXT", 1, 1),
        ("procedure_id", "TEXT", 1, 2),
        ("latest_version", "INTEGER", 1, 0),
    ),
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
_LEGACY_PROCEDURE_SQL = (
    "CREATE TABLE procedure_versions ( tenant_id TEXT NOT NULL, "
    "procedure_id TEXT NOT NULL, version INTEGER NOT NULL, "
    "origin_session_id TEXT NOT NULL, origin_run_id TEXT NOT NULL, "
    "state TEXT NOT NULL, payload TEXT NOT NULL CHECK(length(payload) <= 16384), "
    "PRIMARY KEY (tenant_id, procedure_id, version) ) WITHOUT ROWID"
)
_LEGACY_ACTIVE_PROCEDURE_SQL = (
    "CREATE TABLE active_procedures ( tenant_id TEXT NOT NULL, "
    "procedure_id TEXT NOT NULL, version INTEGER NOT NULL, "
    "PRIMARY KEY (tenant_id, procedure_id) ) WITHOUT ROWID"
)
_LEGACY_PROCEDURE_HEAD_SQL = (
    "CREATE TABLE procedure_version_heads ( tenant_id TEXT NOT NULL, "
    "procedure_id TEXT NOT NULL, latest_version INTEGER NOT NULL CHECK( "
    "latest_version BETWEEN 1 AND 10000 ), "
    "PRIMARY KEY (tenant_id, procedure_id) ) WITHOUT ROWID"
)
_CURRENT_PROCEDURE_HEAD_SQL = (
    'CREATE TABLE "procedure_version_heads" ( tenant_id TEXT NOT NULL, '
    "procedure_id TEXT NOT NULL, latest_version INTEGER NOT NULL CHECK( "
    "latest_version BETWEEN 1 AND 10000 ), "
    "PRIMARY KEY (tenant_id, procedure_id), "
    "FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE "
    ") WITHOUT ROWID"
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
                    if migration.version == 7:
                        await _validate_legacy_procedure_schema(connection)
                    elif migration.version == 8:
                        await _validate_legacy_procedure_head_schema(connection)
                        if applied >= 7:
                            await connection.execute(
                                "DELETE FROM procedure_version_heads AS heads "
                                "WHERE NOT EXISTS (SELECT 1 FROM tenants "
                                "WHERE tenant_id = heads.tenant_id)"
                            )
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
    await _validate_legacy_procedure_schema(connection)
    await _validate_legacy_procedure_head_schema(connection)


async def _validate_legacy_procedure_schema(
    connection: aiosqlite.Connection,
) -> None:
    await _validate_legacy_table_pair(
        connection,
        first_table="procedure_versions",
        first_sql=_LEGACY_PROCEDURE_SQL,
        second_table="active_procedures",
        second_sql=_LEGACY_ACTIVE_PROCEDURE_SQL,
        error="database procedure schema is incompatible",
    )


async def _validate_legacy_procedure_head_schema(
    connection: aiosqlite.Connection,
) -> None:
    await _validate_legacy_table(
        connection,
        table="procedure_version_heads",
        expected_sql=_LEGACY_PROCEDURE_HEAD_SQL,
        error="database procedure version head schema is incompatible",
    )


async def _validate_legacy_table_pair(
    connection: aiosqlite.Connection,
    *,
    first_table: str,
    first_sql: str,
    second_table: str,
    second_sql: str,
    error: str,
) -> None:
    rows = await (
        await connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name COLLATE NOCASE IN (?, ?)",
            (first_table, second_table),
        )
    ).fetchall()
    if not rows:
        return
    if {row[0] for row in rows} != {first_table, second_table}:
        raise MigrationError(error)
    await _validate_legacy_table(
        connection,
        table=first_table,
        expected_sql=first_sql,
        error=error,
    )
    await _validate_legacy_table(
        connection,
        table=second_table,
        expected_sql=second_sql,
        error=error,
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
    for table in (
        "procedure_versions",
        "active_procedures",
        "procedure_version_heads",
    ):
        rows = await (
            await connection.execute(f'PRAGMA table_info("{table}")')
        ).fetchall()
        if tuple(row[1] for row in rows) != _REQUIRED_COLUMNS[table]:
            raise MigrationError("database procedure schema is incompatible")
        if (
            table == "active_procedures"
            and _table_layout(rows) != (_PROCEDURE_LAYOUTS[table])
        ):
            raise MigrationError("database active procedure schema is incompatible")

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
    if not _is_exact_composite_cascade(
        semantic_foreign_keys,
        referenced_table="sessions",
        columns=(
            ("tenant_id", "tenant_id"),
            ("origin_session_id", "session_id"),
        ),
    ):
        raise MigrationError("database semantic fact schema is incompatible")

    procedure_foreign_keys = await (
        await connection.execute('PRAGMA foreign_key_list("procedure_versions")')
    ).fetchall()
    if not _is_exact_composite_cascade(
        procedure_foreign_keys,
        referenced_table="sessions",
        columns=(
            ("tenant_id", "tenant_id"),
            ("origin_session_id", "session_id"),
        ),
    ):
        raise MigrationError("database procedure schema is incompatible")

    active_foreign_keys = await (
        await connection.execute('PRAGMA foreign_key_list("active_procedures")')
    ).fetchall()
    if not _is_exact_composite_cascade(
        active_foreign_keys,
        referenced_table="procedure_versions",
        columns=(
            ("tenant_id", "tenant_id"),
            ("procedure_id", "procedure_id"),
            ("version", "version"),
        ),
    ):
        raise MigrationError("database active procedure schema is incompatible")

    procedure_violations = await (
        await connection.execute('PRAGMA foreign_key_check("procedure_versions")')
    ).fetchall()
    if procedure_violations:
        raise MigrationError("database procedure schema is incompatible")
    active_violations = await (
        await connection.execute('PRAGMA foreign_key_check("active_procedures")')
    ).fetchall()
    if active_violations:
        raise MigrationError("database active procedure schema is incompatible")
    active_rows = await (
        await connection.execute(
            "SELECT versions.tenant_id, versions.procedure_id, versions.version, "
            "versions.origin_session_id, versions.origin_run_id, versions.state, "
            "versions.payload FROM active_procedures AS active "
            "JOIN procedure_versions AS versions USING "
            "(tenant_id, procedure_id, version)"
        )
    ).fetchall()
    if any(not _is_canonical_active_procedure(row) for row in active_rows):
        raise MigrationError("database active procedure schema is incompatible")
    for table in ("procedure_versions", "procedure_version_heads"):
        rows = await (
            await connection.execute(f'PRAGMA table_info("{table}")')
        ).fetchall()
        if _table_layout(rows) != _PROCEDURE_LAYOUTS[table]:
            detail = (
                "procedure"
                if table == "procedure_versions"
                else "procedure version head"
            )
            raise MigrationError(f"database {detail} schema is incompatible")

    head_foreign_keys = await (
        await connection.execute('PRAGMA foreign_key_list("procedure_version_heads")')
    ).fetchall()
    if not _is_exact_composite_cascade(
        head_foreign_keys,
        referenced_table="tenants",
        columns=(("tenant_id", "tenant_id"),),
    ):
        raise MigrationError("database procedure version head schema is incompatible")
    head_violations = await (
        await connection.execute('PRAGMA foreign_key_check("procedure_version_heads")')
    ).fetchall()
    if head_violations:
        raise MigrationError("database procedure version head schema is incompatible")
    invalid_head = await (
        await connection.execute(
            "SELECT 1 FROM procedure_version_heads "
            "WHERE typeof(latest_version) IS NOT 'integer' "
            "OR latest_version NOT BETWEEN 1 AND 10000 LIMIT 1"
        )
    ).fetchone()
    if invalid_head is not None:
        raise MigrationError("database procedure version head schema is incompatible")
    head_definition = await (
        await connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'procedure_version_heads' COLLATE NOCASE"
        )
    ).fetchone()
    if (
        head_definition is None
        or head_definition[0] != "procedure_version_heads"
        or type(head_definition[1]) is not str
        or " ".join(head_definition[1].split()) != _CURRENT_PROCEDURE_HEAD_SQL
    ):
        raise MigrationError("database procedure version head schema is incompatible")
    head_behind_history = await (
        await connection.execute(
            "SELECT 1 FROM procedure_versions AS versions LEFT JOIN "
            "procedure_version_heads AS heads USING (tenant_id, procedure_id) "
            "GROUP BY versions.tenant_id, versions.procedure_id "
            "HAVING heads.latest_version IS NULL OR "
            "heads.latest_version < MAX(versions.version) LIMIT 1"
        )
    ).fetchone()
    if head_behind_history is not None:
        raise MigrationError("database procedure version head schema is incompatible")

    queue_foreign_keys = await (
        await connection.execute('PRAGMA foreign_key_list("work_items")')
    ).fetchall()
    if not _is_exact_composite_cascade(
        queue_foreign_keys,
        referenced_table="runs",
        columns=(("tenant_id", "tenant_id"), ("run_id", "run_id")),
    ):
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


def _is_exact_composite_cascade(
    rows: Iterable[Sequence[object]],
    *,
    referenced_table: str,
    columns: tuple[tuple[str, str], ...],
) -> bool:
    groups: dict[int, list[tuple[int, str, str]]] = {}
    for row in rows:
        if (
            len(row) < 7
            or type(row[0]) is not int
            or type(row[1]) is not int
            or row[2] != referenced_table
            or type(row[3]) is not str
            or type(row[4]) is not str
            or type(row[6]) is not str
            or type(row[5]) is not str
            or row[5].upper() != "NO ACTION"
            or row[6].upper() != "CASCADE"
        ):
            return False
        groups.setdefault(row[0], []).append((row[1], row[3], row[4]))
    if len(groups) != 1:
        return False
    group = next(iter(groups.values()))
    group.sort(key=lambda item: item[0])
    return tuple((source, target) for _, source, target in group) == columns and tuple(
        sequence for sequence, _, _ in group
    ) == tuple(range(len(columns)))


def _is_canonical_active_procedure(row: Sequence[object]) -> bool:
    if (
        len(row) != 7
        or any(type(value) is not str for value in (*row[:2], *row[3:6]))
        or type(row[2]) is not int
        or type(row[6]) is not str
    ):
        return False
    try:
        procedure = ProcedureVersion.model_validate_json(row[6], strict=True)
    except (TypeError, ValidationError, ValueError):
        return False
    return (
        procedure.tenant_id == row[0]
        and procedure.procedure_id == row[1]
        and procedure.version == row[2]
        and procedure.origin_session_id == row[3]
        and procedure.origin_run_id == row[4]
        and row[5] == ProcedureReviewState.APPROVED
        and procedure.state is ProcedureReviewState.APPROVED
        and procedure.review is not None
        and procedure.model_dump_json() == row[6]
    )


def _table_layout(rows: Iterable[Sequence[object]]) -> tuple[tuple[object, ...], ...]:
    return tuple((row[1], row[2], row[3], row[5]) for row in rows)


__all__ = ["MigrationError", "migrate", "validate_current_schema"]
