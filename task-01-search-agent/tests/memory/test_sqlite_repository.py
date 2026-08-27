from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from search_agent.memory import (
    ReflectionStorageError,
    RepositoryClosedError,
    SQLiteReflectionRepository,
)

from .helpers import reflection


def test_sqlite_round_trip_survives_reopen_and_context_closes(tmp_path: Path) -> None:
    path = tmp_path / "reflections.sqlite3"
    expected = reflection()

    with SQLiteReflectionRepository(path) as repository:
        repository.put(expected)

    with pytest.raises(RepositoryClosedError, match="closed"):
        repository.get(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-000001",
        )
    with SQLiteReflectionRepository(path) as reopened:
        assert (
            reopened.get(
                tenant_id="tenant-one",
                session_id="session-one",
                run_id="run-000001",
            )
            == expected
        )


def test_corrupted_sqlite_row_fails_safe_without_echo(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    repository = SQLiteReflectionRepository(path)
    repository.put(reflection())
    private_payload = '{"private":"credential-private-sentinel"}'
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE run_reflections SET payload = ?
            WHERE tenant_id = ? AND session_id = ? AND run_id = ?
            """,
            (private_payload, "tenant-one", "session-one", "run-000001"),
        )

    with pytest.raises(ReflectionStorageError) as error:
        repository.get(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-000001",
        )
    assert "credential-private-sentinel" not in str(error.value)
    repository.close()


def test_sqlite_row_payload_cannot_claim_another_scope(tmp_path: Path) -> None:
    path = tmp_path / "scope-corrupt.sqlite3"
    repository = SQLiteReflectionRepository(path)
    repository.put(reflection())
    foreign_payload = reflection(
        tenant_id="tenant-two", run_id="run-foreign"
    ).model_dump_json()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE run_reflections SET payload = ?
            WHERE tenant_id = ? AND session_id = ? AND run_id = ?
            """,
            (foreign_payload, "tenant-one", "session-one", "run-000001"),
        )

    with pytest.raises(ReflectionStorageError, match="stored reflection"):
        repository.get(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-000001",
        )
    repository.close()


def test_incompatible_schema_and_unsafe_paths_are_rejected(tmp_path: Path) -> None:
    wrong_schema = tmp_path / "wrong.sqlite3"
    with sqlite3.connect(wrong_schema) as connection:
        connection.execute("CREATE TABLE run_reflections (payload TEXT)")
    with pytest.raises(ReflectionStorageError, match="schema"):
        SQLiteReflectionRepository(wrong_schema)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ReflectionStorageError, match="regular file"):
        SQLiteReflectionRepository(directory)

    missing_parent = tmp_path / "missing" / "reflections.sqlite3"
    with pytest.raises(ReflectionStorageError, match="parent directory"):
        SQLiteReflectionRepository(missing_parent)

    with pytest.raises(ReflectionStorageError, match="filesystem path"):
        SQLiteReflectionRepository("unsafe.sqlite3")  # type: ignore[arg-type]


def test_sqlite_rejects_symlink_path(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    SQLiteReflectionRepository(target).close()
    link = tmp_path / "linked.sqlite3"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with pytest.raises(ReflectionStorageError, match="regular file"):
        SQLiteReflectionRepository(link)


def test_sqlite_list_reads_only_the_requested_bound(tmp_path: Path) -> None:
    with SQLiteReflectionRepository(tmp_path / "bounded.sqlite3") as repository:
        for number in range(25):
            repository.put(reflection(run_id=f"run-{number:06d}"))

        listed = repository.list_session(
            tenant_id="tenant-one", session_id="session-one", limit=3
        )

    assert tuple(item.run_id for item in listed) == (
        "run-000000",
        "run-000001",
        "run-000002",
    )
