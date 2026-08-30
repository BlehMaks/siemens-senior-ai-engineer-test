"""Reject broken local links in tracked Markdown files."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)]+)\)")
REMOTE_SCHEMES = {"http", "https", "mailto"}


def _tracked_markdown(repo: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return [
        repo / item.decode(errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def _link_path(raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]
    parsed = urlsplit(target)
    if parsed.scheme.lower() in REMOTE_SCHEMES or not parsed.path:
        return None
    return unquote(parsed.path)


def audit_links(repo: Path) -> list[str]:
    """Return file-and-line findings for missing local Markdown targets."""

    root = repo.resolve()
    findings: list[str] = []
    for document in _tracked_markdown(root):
        text = document.read_text(encoding="utf-8")
        fence: str | None = None
        for line_number, line in enumerate(text.splitlines(), start=1):
            marker = line.lstrip()[:3]
            if fence is None and marker in {"```", "~~~"}:
                fence = marker
                continue
            if fence == marker:
                fence = None
                continue
            if fence is not None:
                continue
            for match in MARKDOWN_LINK.finditer(line):
                raw_path = _link_path(match.group("target"))
                if raw_path is None:
                    continue
                target = (
                    root / raw_path.lstrip("/")
                    if raw_path.startswith("/")
                    else document.parent / raw_path
                )
                if not target.exists():
                    findings.append(
                        f"{document.relative_to(root)}:{line_number}: {raw_path}"
                    )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    findings = audit_links(args.repo)
    if findings:
        print("Documentation link audit failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print("Documentation link audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
