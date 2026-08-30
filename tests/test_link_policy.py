from __future__ import annotations

import runpy
import subprocess
from pathlib import Path

import pytest
from scripts.audit_links import audit_links, main


def _repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _track(repo: Path, name: str, content: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--", name], cwd=repo, check=True)


def test_reports_missing_relative_target_and_line(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _track(repo, "docs/guide.md", "first\n[missing](other.md)\n")

    assert audit_links(repo) == ["docs/guide.md:2: other.md"]


def test_accepts_local_anchor_remote_and_encoded_paths(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _track(repo, "docs/a file.md", "target\n")
    _track(
        repo,
        "README.md",
        "[anchor](#start) [web](https://example.com) [local](<docs/a%20file.md>)\n",
    )

    assert audit_links(repo) == []


def test_accepts_repository_absolute_path_and_title(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _track(repo, "target.md", "target\n")
    _track(repo, "docs/guide.md", '[root](/target.md "title")\n')

    assert audit_links(repo) == []


def test_ignores_links_inside_fenced_examples(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _track(repo, "README.md", "```markdown\n[example](missing.md)\n```\n")

    assert audit_links(repo) == []


def test_cli_reports_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repository(tmp_path)
    _track(repo, "README.md", "[missing](nope.md)\n")

    assert main(["--repo", str(repo)]) == 1
    assert "Documentation link audit failed" in capsys.readouterr().out


def test_cli_reports_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repository(tmp_path)
    _track(repo, "README.md", "[web](mailto:reviewer@example.com)\n")

    assert main(["--repo", str(repo)]) == 0
    assert "Documentation link audit passed" in capsys.readouterr().out


def test_script_entry_point_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    _track(repo, "README.md", "English\n")
    monkeypatch.setattr("sys.argv", ["audit_links.py", "--repo", str(repo)])

    with pytest.raises(SystemExit, match="0"):
        runpy.run_path("scripts/audit_links.py", run_name="__main__")
