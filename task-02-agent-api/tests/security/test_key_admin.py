from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest

import agent_api.security.key_admin as key_admin
from agent_api.security.key_admin import main
from agent_api.storage import (
    SQLiteKeyHashRepository,
    SQLiteTenantRepository,
    TenantRecord,
    migrate,
)


def _pepper() -> str:
    return base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")


def test_key_admin_create_rotate_revoke_prints_plaintext_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "keys.sqlite3"
    monkeypatch.setenv("AGENT_API_KEY_PEPPER", _pepper())

    assert (
        main(
            [
                "--db",
                str(path),
                "create",
                "--tenant-id",
                "tenant-one",
                "--scope",
                "runs:read",
            ]
        )
        == 0
    )
    created = capsys.readouterr()
    first_key = created.out.strip()
    assert first_key.startswith("sai.v1.tenant-one.key-")
    assert first_key not in created.err
    assert first_key.encode() not in path.read_bytes()

    assert (
        main(
            [
                "--db",
                str(path),
                "create",
                "--tenant-id",
                "tenant-one",
                "--scope",
                "runs:read",
            ]
        )
        == 0
    )
    second_created = capsys.readouterr().out.strip()
    assert second_created.startswith("sai.v1.tenant-one.key-")
    assert second_created != first_key

    monkeypatch.setenv("AGENT_API_AUTHORIZATION", f"Bearer {first_key}")
    assert (
        main(
            [
                "--db",
                str(path),
                "rotate",
                "--scope",
                "runs:write",
            ]
        )
        == 0
    )
    rotated = capsys.readouterr()
    second_key = rotated.out.strip()
    assert second_key.startswith("sai.v1.tenant-one.key-")
    assert second_key != first_key
    assert first_key not in rotated.err
    assert second_key not in rotated.err
    assert second_key.encode() not in path.read_bytes()

    monkeypatch.setenv("AGENT_API_AUTHORIZATION", f"Bearer {second_key}")
    assert (
        main(
            [
                "--db",
                str(path),
                "revoke",
            ]
        )
        == 0
    )
    revoked = capsys.readouterr()
    assert revoked.out == ""
    assert second_key not in revoked.err

    assert main(["--db", str(path), "revoke"]) == 0
    assert capsys.readouterr().out == ""


def test_key_admin_requires_secret_input_outside_process_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "keys.sqlite3"
    monkeypatch.setenv("AGENT_API_KEY_PEPPER", _pepper())

    with pytest.raises(SystemExit, match="authorization environment variable"):
        main(["--db", str(path), "revoke"])


def test_key_admin_cloud_backend_runs_full_lifecycle_without_local_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    surrogate = tmp_path / "shared-store-surrogate.sqlite3"
    asyncio.run(migrate(surrogate))
    asyncio.run(
        SQLiteTenantRepository(surrogate).put(
            TenantRecord(tenant_id="tenant-one", created_at=datetime.now(UTC))
        )
    )
    selected: list[tuple[str, str]] = []

    def cloud_repository(*, project: str, database: str) -> SQLiteKeyHashRepository:
        selected.append((project, database))
        return SQLiteKeyHashRepository(surrogate)

    async def reject_local_migration(path: Path) -> None:
        del path
        raise AssertionError("cloud key administration must not migrate SQLite")

    monkeypatch.setattr(key_admin, "_cloud_key_repository", cloud_repository)
    monkeypatch.setattr(key_admin, "migrate", reject_local_migration)
    monkeypatch.setenv("AGENT_API_KEY_PEPPER", _pepper())
    backend = [
        "--gcp-project",
        "project-one",
        "--firestore-database",
        "database-one",
    ]

    assert (
        main([*backend, "create", "--tenant-id", "tenant-one", "--scope", "runs:read"])
        == 0
    )
    first_key = capsys.readouterr().out.strip()
    monkeypatch.setenv("AGENT_API_AUTHORIZATION", f"Bearer {first_key}")
    assert main([*backend, "rotate", "--scope", "runs:write"]) == 0
    second_key = capsys.readouterr().out.strip()
    monkeypatch.setenv("AGENT_API_AUTHORIZATION", f"Bearer {second_key}")
    assert main([*backend, "revoke"]) == 0
    assert capsys.readouterr().out == ""

    assert selected == [("project-one", "database-one")] * 3
    assert first_key != second_key
    assert first_key.encode() not in surrogate.read_bytes()
    assert second_key.encode() not in surrogate.read_bytes()
