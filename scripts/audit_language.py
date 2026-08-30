"""Reject Cyrillic text in tracked, UTF-encoded repository files."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

CYRILLIC = re.compile(r"[\u0400-\u052f]")


def _tracked_paths(repo: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return [
        repo / item.decode(errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def _decode_text(content: bytes) -> str | None:
    """Decode common repository text encodings and ignore binary content."""

    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return content.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\0" in content:
        return None
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def audit_language(repo: Path) -> list[str]:
    """Return file-and-line findings for Cyrillic text in tracked files."""

    root = repo.resolve()
    findings: list[str] = []
    for path in _tracked_paths(root):
        if not path.is_file():
            continue
        text = _decode_text(path.read_bytes())
        if text is None:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if CYRILLIC.search(line):
                findings.append(f"{path.relative_to(root)}:{line_number}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    findings = audit_language(args.repo)
    if findings:
        print("Language audit failed; Cyrillic text found:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print("Language audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
