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


_MIGRATIONS = (_bundled_migration(1, "initial", "001_initial.sql"),)
_CREATE_LEDGER = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
"""


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


__all__ = ["MigrationError", "migrate"]
