from __future__ import annotations

import subprocess
from pathlib import Path

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


def test_cli_passes_for_english_repository(tmp_path: Path, capsys: object) -> None:
    repo = _repository(tmp_path)
    _track(repo, "README.md", b"English only\n")

    assert main(["--repo", str(repo)]) == 0
    assert "Language audit passed." in capsys.readouterr().out  # type: ignore[attr-defined]
