from __future__ import annotations

import asyncio
import base64
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import agent_api.security.key_admin as key_admin
from agent_api.security import parse_authorization_header
from agent_api.security.key_admin import main
from agent_api.storage import (
    ApiKeyHashRecord,
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


def test_key_admin_writes_protected_recovery_output_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "generated.key"

    class FailingRepository:
        async def put(self, record: ApiKeyHashRecord) -> bool:
            del record
            raise RuntimeError("persistence failed after key recovery output")

    monkeypatch.setattr(
        key_admin,
        "_cloud_key_repository",
        lambda **_: FailingRepository(),
    )
    monkeypatch.setenv("AGENT_API_KEY_PEPPER", _pepper())

    with pytest.raises(RuntimeError, match="persistence failed"):
        main(
            [
                "--gcp-project",
                "project-one",
                "create",
                "--tenant-id",
                "tenant-one",
                "--scope",
                "runs:read",
                "--ttl-seconds",
                "900",
                "--output-file",
                str(output),
            ]
        )

    assert (
        output.read_text(encoding="utf-8").strip().startswith("sai.v1.tenant-one.key-")
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert capsys.readouterr().out == ""


def test_protected_output_supports_a_near_name_maximum_destination(
    tmp_path: Path,
) -> None:
    output = tmp_path / ("k" * 250)
    output.write_text("filesystem accepts this component", encoding="utf-8")
    output.unlink()

    key_admin._write_plaintext_file(output, "temporary-key")

    assert output.read_text(encoding="utf-8") == "temporary-key\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_parent_directory_replacement_fails_without_plaintext_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    displaced = tmp_path / "displaced"
    output = output_directory / "generated.key"
    real_open = os.open
    replaced = False

    def replace_parent(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if target == output.name and dir_fd is not None and not replaced:
            output_directory.rename(displaced)
            output_directory.mkdir()
            output.write_text("concurrent-owner\n", encoding="utf-8")
            replaced = True
        return real_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_parent)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert output.read_text(encoding="utf-8") == "concurrent-owner\n"
    assert not (displaced / output.name).exists()
    cleanup = list(displaced.glob(".api-key-cleanup-*"))
    assert len(cleanup) == 1
    assert (cleanup[0] / "owned").read_bytes() == b""


def test_key_admin_protected_output_sets_a_bounded_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "keys.sqlite3"
    output = tmp_path / "generated.key"
    before = datetime.now(UTC)
    monkeypatch.setenv("AGENT_API_KEY_PEPPER", _pepper())

    assert (
        main(
            [
                "--db",
                str(database),
                "create",
                "--tenant-id",
                "tenant-one",
                "--scope",
                "runs:read",
                "--ttl-seconds",
                "900",
                "--output-file",
                str(output),
            ]
        )
        == 0
    )

    plaintext = output.read_text(encoding="utf-8").strip()
    credentials = parse_authorization_header(f"Bearer {plaintext}")
    record = asyncio.run(
        SQLiteKeyHashRepository(database).get(
            tenant_id=credentials.tenant_id,
            key_id=credentials.key_id,
        )
    )
    assert record is not None and record.expires_at is not None
    assert before + timedelta(seconds=899) <= record.expires_at
    assert record.expires_at <= datetime.now(UTC) + timedelta(seconds=901)
    assert capsys.readouterr().out == ""


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
