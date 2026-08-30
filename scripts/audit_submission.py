"""Fail when the Git submission index contains private or unsafe artifacts."""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import tokenize
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
    r"(?:api[_-]?key|password|secret|token|credential))(?P=key_quote)"
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
TEMPLATE_INTERPOLATION = re.compile(
    r"(?<!\$)(?:\$\{\{[^{}\r\n]+\}\}|\$\{[^{}\r\n]+\})"
)
TERRAFORM_INTERPOLATION = TEMPLATE_INTERPOLATION
BRACED_INTERPOLATION = re.compile(r"\{[A-Za-z_][^{}\r\n]*\}")
PYTHON_REFERENCE = r"[A-Za-z_][A-Za-z0-9_.]*(?:\[[^\]\r\n]+\])*"
PYTHON_TRAILING_REFERENCE = re.compile(rf"{PYTHON_REFERENCE}\s*,?\s*[\])}}]+")
PYTHON_REFERENCE_TUPLE = re.compile(rf"\(\s*{PYTHON_REFERENCE}\s*,?\s*\)")
PYTHON_NONE_DEFAULT = re.compile(
    r"[A-Za-z_][A-Za-z0-9_. \[\]|,]*=\s*None(?=\s*(?:[,)]|$))"
)
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

    if Path(path).suffix.lower() == ".py":
        parsed = _python_contains_credential_assignment(content)
        if parsed is not None:
            # Keep exercising the conservative line parser: it remains the fallback
            # for syntactically incomplete Python files and protects its regressions.
            _contains_credential_assignment_linewise(path, content)
            return parsed
        for line in _decode_python_source(content).splitlines():
            recovered = _python_contains_credential_assignment(line.encode())
            if recovered:
                return True
    return _contains_credential_assignment_linewise(path, content)


def _contains_credential_assignment_linewise(path: str, content: bytes) -> bool:
    """Scan configuration lines when a structured parser is unavailable."""

    suffix = Path(path).suffix.lower()
    decoded = (
        _decode_python_source(content)
        if suffix == ".py"
        else content.decode("utf-8", errors="replace")
    )
    lines = decoded.splitlines()
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
            if python_context and (
                PYTHON_TRAILING_REFERENCE.fullmatch(unquoted)
                or PYTHON_REFERENCE_TUPLE.fullmatch(unquoted)
                or PYTHON_NONE_DEFAULT.match(unquoted)
            ):
                continue
            if python_context:
                integer_literal = _python_integer_literal_contains_credential(unquoted)
                if integer_literal is not None:
                    if integer_literal:
                        return True
                    continue
            if python_context and _python_tuple_contains_literal(value):
                return True
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


def _python_contains_credential_assignment(content: bytes) -> bool | None:
    source = _decode_python_source(content)
    try:
        tree = ast.parse(content)
    except (MemoryError, RecursionError, SyntaxError, ValueError):
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(
                _python_assignment_contains_literal(target, node.value, source)
                for target in node.targets
            ):
                return True
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            if node.value is not None and _python_assignment_contains_literal(
                node.target, node.value, source
            ):
                return True
        elif isinstance(node, ast.AugAssign):
            if _python_assignment_contains_literal(node.target, node.value, source):
                return True
        elif isinstance(node, ast.keyword):
            if (
                node.arg is not None
                and _python_name_is_credential(node.arg)
                and _python_value_contains_literal(node.value, source)
            ):
                return True
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and _python_name_is_credential(key.value)
                    and _python_value_contains_literal(value, source)
                ):
                    return True
        elif isinstance(node, ast.arguments):
            positional = [*node.posonlyargs, *node.args]
            positional_defaults = zip(
                positional[-len(node.defaults) :] if node.defaults else (),
                node.defaults,
                strict=True,
            )
            keyword_defaults = (
                (argument, default)
                for argument, default in zip(
                    node.kwonlyargs, node.kw_defaults, strict=True
                )
                if default is not None
            )
            if any(
                _python_name_is_credential(argument.arg)
                and _python_value_contains_literal(default, source)
                for argument, default in (*positional_defaults, *keyword_defaults)
            ):
                return True
    return False


def _python_name_is_credential(name: str) -> bool:
    return (
        re.search(
            r"(?:api[_-]?key|password|secret|token|credential)$",
            name,
            re.IGNORECASE,
        )
        is not None
    )


def _decode_python_source(content: bytes) -> str:
    """Decode Python bytes using the same encoding declaration rules as Python."""

    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(content).readline)
    except (SyntaxError, UnicodeDecodeError):
        encoding = "utf-8-sig"
    try:
        return content.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return content.decode("utf-8-sig", errors="replace")


def _python_target_is_credential(target: ast.expr) -> bool:
    if isinstance(target, ast.Name):
        return _python_name_is_credential(target.id)
    if isinstance(target, ast.Attribute):
        return _python_name_is_credential(target.attr)
    if isinstance(target, ast.Subscript):
        key = target.slice
        return (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and _python_name_is_credential(key.value)
        )
    if isinstance(target, (ast.List, ast.Tuple)):
        return any(_python_target_is_credential(item) for item in target.elts)
    if isinstance(target, ast.Starred):
        return _python_target_is_credential(target.value)
    return False


def _python_assignment_contains_literal(
    target: ast.expr, value: ast.expr, source: str
) -> bool:
    if not _python_target_is_credential(target):
        return False
    if isinstance(value, ast.IfExp):
        pending = [value.body, value.orelse]
        while pending:
            alternative = pending.pop()
            if isinstance(alternative, ast.IfExp):
                pending.extend((alternative.body, alternative.orelse))
            elif _python_assignment_contains_literal(target, alternative, source):
                return True
        return False
    if isinstance(target, (ast.List, ast.Tuple)) and isinstance(
        value, (ast.List, ast.Tuple)
    ):
        return _python_sequence_assignment_contains_literal(
            target.elts, value.elts, source
        )
    return _python_target_is_credential(target) and _python_value_contains_literal(
        value, source
    )


def _python_sequence_assignment_contains_literal(
    targets: list[ast.expr], values: list[ast.expr], source: str
) -> bool:
    return any(
        _python_expanded_sequence_assignment_contains_literal(
            targets, expanded_values, source
        )
        for expanded_values, _ in _python_expand_static_starred_values(values)
    )


def _python_rebound_names(values: list[ast.expr]) -> set[str]:
    names: set[str] = set()
    pending: list[ast.AST] = list(values)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
            pending.append(node.value)
            continue
        if isinstance(node, ast.Lambda):
            pending.extend(node.args.defaults)
            pending.extend(
                default for default in node.args.kw_defaults if default is not None
            )
            continue
        if isinstance(node, ast.BoolOp):
            for operand in node.values:
                pending.append(operand)
                truth = _python_static_truth(operand)
                if isinstance(node.op, ast.And) and truth is False:
                    break
                if isinstance(node.op, ast.Or) and truth is True:
                    break
            continue
        if isinstance(node, ast.IfExp):
            pending.append(node.test)
            truth = _python_static_truth(node.test)
            if truth is True:
                pending.append(node.body)
            elif truth is False:
                pending.append(node.orelse)
            else:
                pending.extend((node.body, node.orelse))
            continue
        if isinstance(node, ast.GeneratorExp):
            if node.generators:
                pending.append(node.generators[0].iter)
            continue
        pending.extend(ast.iter_child_nodes(node))
    return names


def _python_static_truth(value: ast.expr) -> bool | None:
    results: dict[int, bool | None] = {}
    pending: list[tuple[ast.expr, bool]] = [(value, False)]
    while pending:
        node, visited = pending.pop()
        if not visited:
            pending.append((node, True))
            if isinstance(node, ast.NamedExpr):
                pending.append((node.value, False))
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                pending.append((node.operand, False))
            elif isinstance(node, ast.BoolOp):
                pending.extend((operand, False) for operand in node.values)
            continue

        if isinstance(node, ast.Constant):
            results[id(node)] = bool(node.value)
        elif isinstance(node, ast.NamedExpr):
            results[id(node)] = results[id(node.value)]
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            operand = results[id(node.operand)]
            results[id(node)] = None if operand is None else not operand
        elif isinstance(node, ast.BoolOp):
            operands = [results[id(operand)] for operand in node.values]
            if isinstance(node.op, ast.And):
                results[id(node)] = (
                    False
                    if False in operands
                    else True
                    if all(item is True for item in operands)
                    else None
                )
            else:
                results[id(node)] = (
                    True
                    if True in operands
                    else False
                    if all(item is False for item in operands)
                    else None
                )
        else:
            results[id(node)] = None
    return results[id(value)]


def _python_expanded_sequence_assignment_contains_literal(
    targets: list[ast.expr], values: list[ast.expr], source: str
) -> bool:

    target_stars = [
        index for index, target in enumerate(targets) if isinstance(target, ast.Starred)
    ]
    value_stars = [
        index for index, value in enumerate(values) if isinstance(value, ast.Starred)
    ]
    target_prefix = target_stars[0] if target_stars else len(targets)
    value_prefix = value_stars[0] if value_stars else len(values)
    prefix_count = min(target_prefix, value_prefix)
    if any(
        _python_assignment_contains_literal(targets[index], values[index], source)
        for index in range(prefix_count)
    ):
        return True

    target_suffix = (
        len(targets) - target_stars[-1] - 1 if target_stars else len(targets)
    )
    value_suffix = len(values) - value_stars[-1] - 1 if value_stars else len(values)
    suffix_count = min(target_suffix, value_suffix)
    if any(
        _python_assignment_contains_literal(targets[-index], values[-index], source)
        for index in range(1, suffix_count + 1)
    ):
        return True

    if not target_stars and not value_stars and len(targets) == len(values):
        return any(
            _python_assignment_contains_literal(target, value, source)
            for target, value in zip(targets, values, strict=True)
        )
    if target_stars and not value_stars:
        star_index = target_stars[0]
        starred_target = targets[star_index]
        assert isinstance(starred_target, ast.Starred)
        assigned_end = len(values) - (len(targets) - star_index - 1)
        return _python_target_is_credential(starred_target.value) and any(
            _python_value_contains_literal(value, source)
            for value in values[star_index:assigned_end]
        )
    return False


def _python_expand_static_starred_values(
    values: list[ast.expr],
    rebound_names: set[str] | None = None,
) -> list[tuple[list[ast.expr], dict[str, bool]]]:
    """Return exact alternatives while preserving unknown starred segments."""

    rebound_names = set() if rebound_names is None else set(rebound_names)
    expanded: list[tuple[list[ast.expr], dict[str, bool]]] = [([], {})]
    for value in values:
        alternatives = _python_static_starred_value_alternatives(value, rebound_names)
        combined: list[tuple[list[ast.expr], dict[str, bool]]] = []
        for prefix, prefix_conditions in expanded:
            for alternative, alternative_conditions in alternatives:
                if any(
                    key in prefix_conditions and prefix_conditions[key] != expected
                    for key, expected in alternative_conditions.items()
                ):
                    continue
                combined.append(
                    (
                        [*prefix, *alternative],
                        {**prefix_conditions, **alternative_conditions},
                    )
                )
        expanded = combined
        rebound_names.update(_python_rebound_names([value]))
    return expanded


def _python_static_starred_value_alternatives(
    value: ast.expr,
    rebound_names: set[str],
) -> list[tuple[list[ast.expr], dict[str, bool]]]:
    if not isinstance(value, ast.Starred):
        return [([value], {})]
    alternatives = _python_sequence_expression_alternatives(value.value, rebound_names)
    return alternatives if alternatives is not None else [([value], {})]


def _python_sequence_expression_alternatives(
    value: ast.expr,
    rebound_names: set[str],
) -> list[tuple[list[ast.expr], dict[str, bool]]] | None:
    if isinstance(value, (ast.List, ast.Tuple)):
        return _python_expand_static_starred_values(value.elts, rebound_names)
    if not isinstance(value, ast.IfExp):
        return None

    branch_rebound_names = set(rebound_names)
    branch_rebound_names.update(_python_rebound_names([value.test]))
    body = _python_sequence_expression_alternatives(value.body, branch_rebound_names)
    orelse = _python_sequence_expression_alternatives(
        value.orelse, branch_rebound_names
    )
    if body is None or orelse is None:
        return None
    condition = (
        f"name:{value.test.id}"
        if isinstance(value.test, ast.Name) and value.test.id not in rebound_names
        else f"node:{id(value.test)}"
    )
    alternatives: list[tuple[list[ast.expr], dict[str, bool]]] = []
    for branch, expected in ((body, True), (orelse, False)):
        for expressions, conditions in branch:
            if condition in conditions and conditions[condition] != expected:
                continue
            alternatives.append((expressions, {**conditions, condition: expected}))
    return alternatives


def _python_value_contains_literal(value: ast.expr, source: str) -> bool:
    del source
    pending: list[ast.expr] = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Constant):
            if isinstance(current.value, (str, bytes)) and len(current.value) >= 8:
                return True
            if type(current.value) is int and abs(current.value) >= 10**15:
                return True
            if (
                type(current.value) in {float, complex}
                and len(str(current.value).replace("_", "")) >= 16
            ):
                return True
            continue
        if isinstance(current, (ast.List, ast.Set, ast.Tuple)):
            pending.extend(current.elts)
            continue
        if isinstance(current, ast.Dict):
            pending.extend(current.values)
            continue
        if isinstance(current, ast.Starred):
            pending.append(current.value)
            continue
        if isinstance(current, ast.JoinedStr):
            if (
                not any(isinstance(item, ast.FormattedValue) for item in current.values)
                and sum(
                    len(item.value)
                    for item in current.values
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
                >= 8
            ):
                return True
            continue
        if isinstance(current, ast.IfExp):
            pending.extend((current.body, current.orelse))
            continue
        if isinstance(current, (ast.BoolOp, ast.BinOp, ast.UnaryOp)):
            pending.extend(
                child
                for child in ast.iter_child_nodes(current)
                if isinstance(child, ast.expr)
            )
    return False


def _python_tuple_contains_literal(value: str) -> bool:
    try:
        expression = ast.parse(value, mode="eval").body
    except (MemoryError, RecursionError, SyntaxError, ValueError):
        return False
    if not isinstance(expression, ast.Tuple):
        return False
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, (str, bytes))
        and len(node.value) >= 8
        for node in ast.walk(expression)
    )


def _python_integer_literal_contains_credential(value: str) -> bool | None:
    try:
        expression = ast.parse(value, mode="eval").body
    except (MemoryError, RecursionError, SyntaxError, ValueError):
        normalized = value.replace("_", "").lstrip("+-")
        if not re.fullmatch(r"(?:0|[1-9][0-9]*)", normalized):
            return None
        threshold = "1000000000000000"
        return len(normalized) > len(threshold) or (
            len(normalized) == len(threshold) and normalized >= threshold
        )

    if isinstance(expression, ast.Constant) and type(expression.value) is int:
        return abs(expression.value) >= 10**15
    if (
        isinstance(expression, ast.UnaryOp)
        and isinstance(expression.op, (ast.UAdd, ast.USub))
        and isinstance(expression.operand, ast.Constant)
        and type(expression.operand.value) is int
    ):
        return abs(expression.operand.value) >= 10**15
    return None


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
