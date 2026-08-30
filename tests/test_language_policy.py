from __future__ import annotations

import runpy
import subprocess
from pathlib import Path

import pytest
from scripts.audit_language import audit_language, main


def _repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _track(repo: Path, name: str, content: bytes) -> None:
    path = repo / name
    path.write_bytes(content)
    subprocess.run(["git", "add", "--", name], cwd=repo, check=True)


def test_reports_cyrillic_file_and_line(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    cyrillic = "".join(
        chr(value) for value in (0x41F, 0x440, 0x438, 0x432, 0x435, 0x442)
    )
    _track(repo, "notes.md", f"first\n{cyrillic}\n".encode())

    assert audit_language(repo) == ["notes.md:2"]


def test_supports_utf16_text(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    cyrillic = "".join(chr(value) for value in (0x442, 0x435, 0x441, 0x442))
    _track(repo, "notes.txt", f"hello\n{cyrillic}\n".encode("utf-16"))

    assert audit_language(repo) == ["notes.txt:2"]


def test_skips_binary_content(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _track(repo, "image.bin", b"\x00\xd0\x9f\xd1\x80\x00")

    assert audit_language(repo) == []


def test_skips_invalid_utf_encodings(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _track(repo, "broken-utf16.txt", b"\xff\xfe\x00")
    _track(repo, "broken-utf8.txt", b"\xff")

    assert audit_language(repo) == []


def test_skips_tracked_file_removed_from_worktree(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _track(repo, "removed.txt", b"English\n")
    (repo / "removed.txt").unlink()

    assert audit_language(repo) == []


def test_cli_passes_for_english_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repository(tmp_path)
    _track(repo, "README.md", b"English only\n")

    assert main(["--repo", str(repo)]) == 0
    assert "Language audit passed." in capsys.readouterr().out


def test_cli_reports_cyrillic_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repository(tmp_path)
    _track(repo, "notes.md", chr(0x410).encode())

    assert main(["--repo", str(repo)]) == 1
    assert "notes.md:1" in capsys.readouterr().out


def test_script_entry_point_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    _track(repo, "README.md", b"English\n")
    monkeypatch.setattr("sys.argv", ["audit_language.py", "--repo", str(repo)])

    with pytest.raises(SystemExit, match="0"):
        runpy.run_path("scripts/audit_language.py", run_name="__main__")
