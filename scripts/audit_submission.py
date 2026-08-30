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
        re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
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
            rb"(?m)(?:^|[\s\"'(=])"
            rb"(?:(?:/Users/|/home/)[A-Za-z0-9._-]+/|/root/"
            rb"|(?i:[A-Z]:\\Users\\[A-Za-z0-9._-]+\\))"
        ),
    ),
)

_CREDENTIAL_ASSIGNMENT_BODY = (
    r"[ \t]*(?:-[ \t]+)?(?:\#[ \t]*)?"
    r"(?:(?:export|local|readonly|declare|typeset)"
    r"(?:[ \t]+(?:--|-[A-Za-z]+))*[ \t]+)?"
    r"(?P<key_quote>[\"']?)(?P<key>[A-Za-z0-9_.-]*"
    r"(?:api[_-]?key|password|secret|token))(?P=key_quote)"
    r"(?:\[[^\]\r\n]+\])?[ \t]*(?::|\+?=)"
)
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?:^|(?<=[{,;]))" + _CREDENTIAL_ASSIGNMENT_BODY,
    re.IGNORECASE,
)
SHELL_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?:^|(?<=[{,;( \t]))" + _CREDENTIAL_ASSIGNMENT_BODY,
    re.IGNORECASE,
)
QUOTED_VALUE = re.compile(r"(?P<prefix>\$|(?i:[bruf]{0,3}))(?P<quote>\"\"\"|'''|\"|')")
SYMBOLIC_VALUE = re.compile(
    r"(?:"
    r"(?:var|local|module|data|settings|google_[A-Za-z0-9_]*|"
    r"aws_[A-Za-z0-9_]*|azurerm_[A-Za-z0-9_]*)\.[A-Za-z0-9_.\[\]\"'-]+|"
    r"[A-Za-z_][A-Za-z0-9_.]*\s*\(.*\)(?:\.[A-Za-z_][A-Za-z0-9_]*\(.*\))*"
    r")",
    re.DOTALL,
)
SHELL_INTERPOLATION = re.compile(
    r"(?<!\\)(?:\\\\)*(?:"
    r"\$\{[^{}\r\n]+\}|"
    r"\$[A-Za-z_][A-Za-z0-9_]*|"
    r"\$[-0-9@*#?$!]|"
    r"\$\(|"
    r"`[^`\r\n]+`"
    r")"
)
TEMPLATE_INTERPOLATION = re.compile(r"(?<!\$)\$\{[^{}\r\n]+\}")
TERRAFORM_INTERPOLATION = TEMPLATE_INTERPOLATION
BRACED_INTERPOLATION = re.compile(r"\{[A-Za-z_][^{}\r\n]*\}")
SHELL_SUFFIXES = {".bash", ".sh", ".zsh"}
TEMPLATE_SUFFIXES = {".env", ".json", ".toml", ".yaml", ".yml"}
SHELL_SHEBANG = re.compile(r"^#![^\r\n]*(?:ba|z)?sh(?:[ \t]|$)")


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
    name = normalized.name.lower()
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


def _strip_inline_comment(value: str) -> str:
    """Remove a trailing configuration comment without excluding `#` in a secret."""

    return re.split(r"[ \t]+#", value, maxsplit=1)[0].strip()


def _is_literal(
    value: str,
    *,
    minimum_length: int,
    interpolation: re.Pattern[str] | None = None,
    reference_context: bool = False,
    braced_interpolation: bool = False,
) -> bool:
    candidate = value.strip()
    if len(candidate) < minimum_length or SYMBOLIC_VALUE.fullmatch(candidate):
        return False
    if interpolation is not None and interpolation.search(candidate):
        return False
    if braced_interpolation and BRACED_INTERPOLATION.search(candidate):
        return False
    if (
        candidate.startswith("$(")
        or (candidate.startswith("`") and candidate.endswith("`"))
        or ") ->" in candidate
    ):
        return False
    if reference_context and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_.]*(?:\[[^\]\r\n]+\])*", candidate
    ):
        return False
    return re.match(r"[A-Za-z_][A-Za-z0-9_.]*\s*\(", candidate) is None


def _closing_quote_index(value: str, quote: str) -> int | None:
    offset = 0
    while True:
        index = value.find(quote, offset)
        if index < 0:
            return None
        preceding_slashes = 0
        cursor = index - 1
        while cursor >= 0 and value[cursor] == "\\":
            preceding_slashes += 1
            cursor -= 1
        if preceding_slashes % 2 == 0:
            return index
        offset = index + len(quote)


def _quoted_literal(
    value: str, following_lines: list[str]
) -> tuple[str, str, str, str] | None:
    quote_match = QUOTED_VALUE.match(value)
    if quote_match is None:
        return None

    quote = quote_match.group("quote")
    remainder = value[quote_match.end() :]
    candidate = remainder
    if len(quote) > 1:
        candidate = "\n".join([remainder, *following_lines])
    closing_index = _closing_quote_index(candidate, quote)
    if closing_index is None:
        return None
    return (
        candidate[:closing_index],
        quote_match.group("prefix").lower(),
        quote,
        candidate[closing_index + len(quote) :].strip(),
    )


def _shell_word(value: str, following_lines: list[str]) -> tuple[str, bool] | None:
    """Return the complete first shell word and whether it performs expansion."""

    source = "\n".join([value, *following_lines])
    literal: list[str] = []
    cursor = 0

    while cursor < len(source):
        char = source[cursor]
        if char in " \t\r\n;":
            break

        ansi_c_quote = source.startswith("$'", cursor)
        locale_quote = source.startswith('$"', cursor)
        if ansi_c_quote or locale_quote:
            quote = source[cursor + 1]
            remainder = source[cursor + 2 :]
            closing = _closing_quote_index(remainder, quote)
            if closing is None:
                return None
            segment = remainder[:closing]
            if locale_quote and SHELL_INTERPOLATION.search(segment):
                return "", True
            literal.append(segment)
            cursor += closing + 3
            continue

        if char == "'":
            closing = source.find("'", cursor + 1)
            if closing < 0:
                return None
            literal.append(source[cursor + 1 : closing])
            cursor = closing + 1
            continue

        if char == '"':
            remainder = source[cursor + 1 :]
            closing = _closing_quote_index(remainder, '"')
            if closing is None:
                return None
            segment = remainder[:closing]
            if SHELL_INTERPOLATION.search(segment):
                return "", True
            literal.append(segment)
            cursor += closing + 2
            continue

        if char == "\\" and cursor + 1 < len(source):
            escaped = source[cursor + 1]
            if escaped != "\n":
                literal.append(escaped)
            cursor += 2
            continue

        if char == "`" or source.startswith(("$(", "<(", ">("), cursor):
            return "", True
        if char == "$" and SHELL_INTERPOLATION.match(source, cursor):
            return "", True

        literal.append(char)
        cursor += 1

    return "".join(literal), False


def _contains_credential_assignment(path: str, content: bytes) -> bool:
    """Recognize committed credential literals while ignoring symbolic references."""

    lines = content.decode("utf-8", errors="replace").splitlines()
    suffix = Path(path).suffix.lower()
    shell_context = suffix in SHELL_SUFFIXES or bool(
        lines and SHELL_SHEBANG.match(lines[0])
    )
    terraform_context = suffix in {".hcl", ".tf"} or path.lower().endswith(".tf.json")
    python_context = suffix == ".py"
    reference_context = shell_context or terraform_context or python_context
    assignment_pattern = (
        SHELL_CREDENTIAL_ASSIGNMENT if shell_context else CREDENTIAL_ASSIGNMENT
    )
    for index, line in enumerate(lines):
        for assignment in assignment_pattern.finditer(line):
            raw_value = line[assignment.end() :]
            if shell_context and (not raw_value or raw_value[0] in " \t"):
                continue
            value = raw_value.strip()
            if shell_context:
                shell_word = _shell_word(value, lines[index + 1 :])
                if shell_word is not None:
                    literal, expands = shell_word
                    if not expands and len(literal) >= 8:
                        return True
                continue

            if re.fullmatch(r"[|>][+-]?[1-9]?", value):
                base_indent = len(line) - len(line.lstrip())
                block_lines: list[str] = []
                block_indent: int | None = None
                for part in lines[index + 1 :]:
                    if not part.strip():
                        block_lines.append("")
                        continue
                    part_indent = len(part) - len(part.lstrip())
                    if block_indent is None:
                        if part_indent <= base_indent:
                            break
                        block_indent = part_indent
                    elif part_indent < block_indent:
                        break
                    block_lines.append(part.strip())
                block = "\n".join(block_lines)
                if _is_literal(
                    block,
                    minimum_length=16,
                    interpolation=(
                        TEMPLATE_INTERPOLATION if suffix in TEMPLATE_SUFFIXES else None
                    ),
                ):
                    return True
                continue

            quoted = _quoted_literal(value, lines[index + 1 :])
            if quoted is not None:
                literal, prefix, quote, trailing = quoted
                interpolation = None
                if shell_context and quote != "'":
                    interpolation = SHELL_INTERPOLATION
                elif terraform_context:
                    interpolation = TERRAFORM_INTERPOLATION
                elif suffix in TEMPLATE_SUFFIXES:
                    interpolation = TEMPLATE_INTERPOLATION
                if _is_literal(
                    literal,
                    minimum_length=8,
                    interpolation=interpolation,
                    braced_interpolation=(
                        "f" in prefix
                        or (python_context and trailing.startswith(".format("))
                    ),
                ):
                    return True
                continue

            unquoted = _strip_inline_comment(value).split(";", 1)[0].strip()
            if python_context and unquoted.endswith(","):
                unquoted = unquoted[:-1].rstrip()
            if _is_literal(
                unquoted,
                minimum_length=16,
                interpolation=(
                    SHELL_INTERPOLATION
                    if shell_context
                    else TERRAFORM_INTERPOLATION
                    if terraform_context
                    else TEMPLATE_INTERPOLATION
                    if suffix in TEMPLATE_SUFFIXES
                    else None
                ),
                reference_context=reference_context,
            ):
                return True

    return False


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
        if _contains_credential_assignment(path, content):
            findings.append(f"credential assignment: {path}")
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
