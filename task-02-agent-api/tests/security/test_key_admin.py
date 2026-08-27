from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agent_api.security.key_admin import main


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
