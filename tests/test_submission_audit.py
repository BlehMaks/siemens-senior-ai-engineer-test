from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.audit_submission import audit_repository


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def seed(*parts: str) -> bytes:
    return "".join(parts).encode()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitignore").write_text(".local/\ninput/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Safe public file.\n", encoding="utf-8")
    git(tmp_path, "add", ".gitignore", "README.md")
    return tmp_path


def test_clean_index_passes(repository: Path) -> None:
    assert audit_repository(repository) == []


@pytest.mark.parametrize(
    ("path", "content", "expected"),
    [
        (".local/plan.md", b"private\n", "forbidden path"),
        (
            "config.py",
            seed("OPEN", 'ROUTER_API_KEY = "not-a-real-key"\n'),
            "OpenRouter",
        ),
        (
            "secret.txt",
            seed("-----BEGIN ", "PRIVATE KEY-----\n"),
            "private key",
        ),
        (
            "notes.md",
            seed("/Us", "ers/example/private/input.csv\n"),
            "absolute user path",
        ),
    ],
)
def test_forbidden_staged_artifact_fails(
    repository: Path, path: str, content: bytes, expected: str
) -> None:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    git(repository, "add", "--force", path)

    assert any(expected in finding for finding in audit_repository(repository))


def test_nonignored_untracked_file_fails(repository: Path) -> None:
    (repository / "orphan.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert audit_repository(repository) == ["untracked public candidate: orphan.py"]


def test_ignored_local_files_do_not_enter_audit(repository: Path) -> None:
    local_file = repository / "input" / "private.csv"
    local_file.parent.mkdir()
    local_file.write_text("private data\n", encoding="utf-8")

    assert audit_repository(repository) == []
