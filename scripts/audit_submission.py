"""Fail when the Git submission index contains private or unsafe artifacts."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath

MAX_PUBLIC_FILE_BYTES = 5 * 1024 * 1024

FORBIDDEN_PARTS = {
    ".local",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".terraform",
    ".uv-cache",
    "__pycache__",
    "artifacts",
    "input",
    "models",
    "runs",
}
FORBIDDEN_SUFFIXES = {
    ".key",
    ".pem",
}
CONTENT_RULES = (
    (
        "private key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    ),
    (
        "credential assignment",
        re.compile(
            rb"(?i)(?:api[_-]?key|password|secret|token)\s*[:=]\s*"
            rb"[\"']?[A-Za-z0-9_./+=-]{16,}[\"']?"
        ),
    ),
    ("OpenRouter credential", re.compile(rb"OPENROUTER_(?:API_)?KEY")),
    (
        "private council artifact",
        re.compile(rb"(?:\.local/)?council/(?:final-plan|prompt|transcript)"),
    ),
    ("hidden prompt artifact", re.compile(rb"BEGIN (?:HIDDEN|SYSTEM) PROMPT")),
    (
        "absolute user path",
        re.compile(
            rb"(?:/Users/|/home/)[A-Za-z0-9._-]+/"
            rb"|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\"
        ),
    ),
)


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _index_entries(raw: bytes) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0].decode()
        entries.append((raw_path.decode(errors="surrogateescape"), mode))
    return entries


def _nul_paths(raw: bytes) -> list[str]:
    return [item.decode(errors="surrogateescape") for item in raw.split(b"\0") if item]


def _path_findings(path: str) -> list[str]:
    normalized = PurePosixPath(path)
    name = normalized.name
    findings: list[str] = []
    if any(part in FORBIDDEN_PARTS for part in normalized.parts):
        findings.append(f"forbidden path: {path}")
    if (
        name == ".env"
        or name.startswith(".env.")
        or ".tfstate." in name
        or name.endswith(".tfstate")
        or any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)
    ):
        findings.append(f"secret or state filename: {path}")
    return findings


def audit_repository(repo: Path) -> list[str]:
    """Audit the exact Git index and report non-ignored untracked files."""

    repo = repo.resolve()
    findings: list[str] = []
    tracked = _index_entries(_git(repo, "ls-files", "--stage", "-z"))

    for path, mode in tracked:
        findings.extend(_path_findings(path))
        content = _git(repo, "show", f":{path}")
        if mode == "120000":
            findings.append(f"tracked symlink: {path}")
            continue
        if len(content) > MAX_PUBLIC_FILE_BYTES:
            findings.append(f"oversized tracked file: {path}")
            continue
        for label, rule in CONTENT_RULES:
            if rule.search(content):
                findings.append(f"{label}: {path}")

    # A release-critical source file that is not in the index cannot appear in the
    # reviewed archive. Ignore rules remain the explicit escape hatch for local data.
    for path in _nul_paths(
        _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    ):
        findings.append(f"untracked public candidate: {path}")

    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    findings = audit_repository(args.repo)
    if findings:
        print("Submission audit failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print("Submission audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
