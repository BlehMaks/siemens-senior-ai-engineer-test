from __future__ import annotations

import pytest
from scripts.audit_submission import _contains_credential_assignment


def _seed(*parts: str) -> bytes:
    return "".join(parts).encode()


@pytest.mark.parametrize(
    "content",
    [
        _seed("def configure(index_tok", "en: str | None = None): ...\n"),
        _seed(
            "def configure(index_tok",
            "en: str | None = None, retries: int = 3): ...\n",
        ),
    ],
)
def test_none_parameter_layouts_are_runtime_structure(content: bytes) -> None:
    compile(content, "config.py", "exec")

    assert not _contains_credential_assignment("config.py", content)


def test_later_literal_after_none_parameter_remains_visible() -> None:
    content = _seed(
        "def configure(index_tok",
        'en: str | None = None, api_key="abcdefghijklmnop"): ...\n',
    )
    compile(content, "config.py", "exec")

    assert _contains_credential_assignment("config.py", content)


@pytest.mark.parametrize(
    "content",
    [
        _seed("call(\n    index_tok", "en=refreshed_token)\n"),
        _seed('config = {\n    "index_tok', 'en": refreshed_token}\n'),
    ],
)
def test_runtime_reference_before_closer_is_not_literal(content: bytes) -> None:
    compile(content, "config.py", "exec")

    assert not _contains_credential_assignment("config.py", content)


@pytest.mark.parametrize("value", ['("12345678",)', '(b"12345678",)'])
def test_short_tuple_literal_remains_visible(value: str) -> None:
    content = _seed("index_tok", f"en = {value}\n")
    compile(content, "config.py", "exec")

    assert _contains_credential_assignment("config.py", content)


@pytest.mark.parametrize("literal", ['"12345678"', 'b"12345678"'])
@pytest.mark.parametrize(
    "template",
    [
        "def configure():\n    {key} = (\n        {literal},\n    )\n",
        "def configure():\n    return call(\n        {key}=({literal},))\n",
        "def configure():\n    return call({key}=({literal},))\n",
    ],
)
def test_python_tuple_literals_are_detected_in_complete_syntax(
    literal: str, template: str
) -> None:
    key = _seed("index_tok", "en").decode()
    content = template.format(key=key, literal=literal).encode()
    compile(content, "config.py", "exec")

    assert _contains_credential_assignment("config.py", content)


@pytest.mark.parametrize(
    "content",
    [
        _seed("call(\n    index_tok", "en=(refreshed_token))\n"),
        _seed('config = {\n    "index_tok', 'en": (refreshed_token)}\n'),
    ],
)
def test_grouped_runtime_references_before_closers_are_not_literals(
    content: bytes,
) -> None:
    compile(content, "config.py", "exec")

    assert not _contains_credential_assignment("config.py", content)


def test_unannotated_none_default_with_later_syntax_is_not_literal() -> None:
    content = _seed(
        "def configure(\n    index_tok",
        "en=None, retries: int = 3,\n): ...\n",
    )
    compile(content, "config.py", "exec")

    assert not _contains_credential_assignment("config.py", content)


def test_augmented_assignment_literal_is_detected() -> None:
    content = _seed(
        "tok",
        "en = runtime_token\n",
        "tok",
        'en += "12345678"\n',
    )
    compile(content, "config.py", "exec")

    assert _contains_credential_assignment("config.py", content)


@pytest.mark.parametrize("literal", ['"12345678"', 'b"12345678"'])
def test_literal_conditional_branch_is_detected(literal: str) -> None:
    content = _seed("tok", f"en = {literal} if enabled else runtime_token\n")
    compile(content, "config.py", "exec")

    assert _contains_credential_assignment("config.py", content)


@pytest.mark.parametrize(
    "content",
    [
        _seed(
            "harmless, tok",
            'en = "12345678", runtime_token\n',
        ),
        _seed(
            "tok",
            'en, harmless = runtime_token, "12345678"\n',
        ),
    ],
)
def test_parallel_assignment_preserves_target_value_pairing(content: bytes) -> None:
    compile(content, "config.py", "exec")

    assert not _contains_credential_assignment("config.py", content)


def test_starred_credential_target_is_detected() -> None:
    content = _seed("*api_k", 'ey, tail = ("12345678", "tail")\n')
    compile(content, "config.py", "exec")

    assert _contains_credential_assignment("config.py", content)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            _seed(
                "result = call(\n    index_tok",
                'en=(\n        "12345678",\n    ),\n)\n',
            ),
            True,
        ),
        (_seed("call(\n    index_tok", "en=(runtime_token))\n"), False),
        (_seed("call(\n    index_tok", "en=(build_token()))\n"), False),
    ],
)
def test_utf8_bom_uses_structured_python_detection(
    source: bytes, expected: bool
) -> None:
    content = b"\xef\xbb\xbf" + source
    compile(content, "config.py", "exec")

    assert _contains_credential_assignment("config.py", content) is expected


@pytest.mark.parametrize(
    "value",
    [
        '[*"12345678"]',
        '(*[b"12345678"],)',
        '{*"12345678"}',
    ],
)
def test_starred_value_does_not_hide_literal(value: str) -> None:
    content = _seed("tok", f"en = {value}\n")
    compile(content, "config.py", "exec")

    assert _contains_credential_assignment("config.py", content)


def test_bom_invalid_python_fallback_scans_first_line() -> None:
    content = b"\xef\xbb\xbf" + _seed("tok", 'en = "12345678"\nif (\n')

    assert _contains_credential_assignment("config.py", content)


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("123_456_789_012_345", False),
        ("1_234_567_890_123_456", True),
    ],
)
def test_declared_encoding_keeps_numeric_threshold_stable(
    number: str, expected: bool
) -> None:
    content = (
        _seed(
            '# coding: latin-1\nlabel = "',
            "é".encode("latin-1").decode("latin-1"),
            '"\ntok',
            f"en = {number}\n",
        )
        .decode("utf-8")
        .encode("latin-1")
    )
    compile(content, "config.py", "exec")

    assert _contains_credential_assignment("config.py", content) is expected


@pytest.mark.parametrize(
    "content",
    [
        _seed("*api_k", 'ey, harmless = runtime_token, "12345678"\n'),
        _seed("tok", 'en, middle, tail = *runtime_values, "12345678"\n'),
        _seed(
            "((head, *api_k",
            'ey), tail) = (("12345678", runtime_token), runtime_tail)\n',
        ),
    ],
)
def test_starred_unpacking_does_not_attribute_sibling_literals(
    content: bytes,
) -> None:
    compile(content, "config.py", "exec")

    assert not _contains_credential_assignment("config.py", content)


@pytest.mark.parametrize(
    ("source", "expected_token"),
    [
        (_seed("tok", 'en, tail = *["12345678"], "tail"\n').decode(), "12345678"),
        (_seed("head, tok", 'en = "head", *["12345678"]\n').decode(), "12345678"),
        (
            _seed(
                "((head, *tok",
                'en), tail) = (("head", *["12345678"]), "tail")\n',
            ).decode(),
            ["12345678"],
        ),
    ],
)
def test_fixed_starred_rhs_preserves_credential_attribution(
    source: str, expected_token: str | list[str]
) -> None:
    namespace: dict[str, object] = {}
    exec(source, namespace)
    assert namespace["token"] == expected_token

    assert _contains_credential_assignment("config.py", source.encode())


def test_large_hexadecimal_literal_does_not_crash() -> None:
    content = _seed("tok", "en = 0x", "f" * 4000, "\n")
    compile(content, "config.py", "exec")

    assert _contains_credential_assignment("config.py", content)


def test_incomplete_python_fallback_uses_declared_encoding() -> None:
    source = _seed("# coding: cp1252\ntok", 'en = "Ã©Ã©Ã©Ã©"\nif (\n').decode()
    assert len("Ã©Ã©Ã©Ã©") == 8

    assert _contains_credential_assignment("config.py", source.encode("cp1252"))


def test_conditional_sequence_preserves_target_value_pairing() -> None:
    source = _seed(
        "tok",
        'en, harmless = (runtime_token, "12345678") ',
        'if enabled else (other_token, "abcdefgh")\n',
    ).decode()
    for enabled, expected_token in ((True, "a"), (False, "b")):
        namespace = {
            "enabled": enabled,
            "runtime_token": "a",
            "other_token": "b",
        }
        exec(source, namespace)
        assert namespace["token"] == expected_token

    assert not _contains_credential_assignment("config.py", source.encode())


@pytest.mark.parametrize(
    ("source", "runtime_values"),
    [
        (
            _seed("head, tok", 'en = *runtime_values, *["12345678"]\n').decode(),
            ["head"],
        ),
        (
            _seed("tok", 'en, tail = *["12345678"], *runtime_values\n').decode(),
            ["tail"],
        ),
        (
            _seed(
                "((head, tok",
                'en), tail) = ((*runtime_values, *["12345678"]), "tail")\n',
            ).decode(),
            ["head"],
        ),
    ],
)
def test_known_star_next_to_unknown_star_keeps_exact_pairing(
    source: str, runtime_values: list[str]
) -> None:
    namespace: dict[str, object] = {"runtime_values": runtime_values}
    exec(source, namespace)
    assert namespace["token"] == "12345678"

    assert _contains_credential_assignment("config.py", source.encode())


def test_conditional_static_starred_values_are_inspected() -> None:
    source = _seed(
        "tok",
        'en, tail = *(["12345678"] if enabled else [runtime_token]), "tail"\n',
    ).decode()

    assert _contains_credential_assignment("config.py", source.encode())


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (f"-{10**15 - 1}", False),
        (hex(10**15), True),
        (oct(10**15 - 1), False),
        (bin(10**15 - 1), False),
    ],
)
def test_incomplete_integer_literals_use_magnitude(number: str, expected: bool) -> None:
    content = _seed("tok", f"en = {number}\nif (\n")

    assert _contains_credential_assignment("config.py", content) is expected


def test_nested_static_starred_conditionals_are_inspected() -> None:
    source = _seed(
        "tok",
        'en, tail = *(["12345678"] if first else ',
        '([runtime_token] if second else [other_token])), "tail"\n',
    ).decode()

    assert _contains_credential_assignment("config.py", source.encode())


def test_correlated_starred_conditionals_keep_branch_pairing() -> None:
    source = _seed(
        "tok",
        "en, harmless = *([runtime_token] if enabled else []), ",
        '*(["12345678"] if enabled else [runtime_token, "12345678"])\n',
    ).decode()
    for enabled in (True, False):
        namespace = {"enabled": enabled, "runtime_token": "r"}
        exec(source, namespace)
        assert namespace["token"] == "r"
        assert namespace["harmless"] == "12345678"

    assert not _contains_credential_assignment("config.py", source.encode())


def test_rebound_condition_does_not_force_impossible_branch_correlation() -> None:
    source = _seed(
        "enabled = True\nhead, tok",
        "en, sibling, tail = ",
        "*([(enabled := False)] if enabled else [runtime_a, runtime_b]), ",
        '*([runtime_token] if enabled else ["12345678", (enabled := True)]), ',
        "*([runtime_tail] if enabled else [])\n",
    ).decode()
    namespace = {
        "runtime_a": "a",
        "runtime_b": "b",
        "runtime_token": "rt",
        "runtime_tail": "tail",
    }
    exec(source, namespace)
    assert namespace["token"] == "12345678"

    assert _contains_credential_assignment("config.py", source.encode())


def test_lambda_local_rebound_does_not_change_outer_branch_pairing() -> None:
    source = _seed(
        "tok",
        "en, *rest = *([runtime_token] if enabled else []), ",
        '*(["12345678"] if enabled else [runtime_token, "12345678"]), ',
        "(lambda: (enabled := False))\n",
    ).decode()
    for enabled in (True, False):
        namespace = {"enabled": enabled, "runtime_token": "r"}
        exec(source, namespace)
        assert namespace["token"] == "r"

    assert not _contains_credential_assignment("config.py", source.encode())


def test_lambda_default_rebound_changes_outer_branch_pairing() -> None:
    source = _seed(
        "enabled = True\nhead, tok",
        "en = *([(lambda value=(enabled := False): value)] ",
        "if enabled else [runtime_head]), ",
        '*([runtime_token] if enabled else ["12345678"])\n',
    ).decode()
    namespace = {"runtime_head": "head", "runtime_token": "runtime"}
    exec(source, namespace)
    assert namespace["token"] == "12345678"

    assert _contains_credential_assignment("config.py", source.encode())


def test_rebound_collection_handles_deep_valid_expression() -> None:
    expression = " + ".join("runtime_value" for _ in range(500))
    source = _seed("tok", f"en = {expression}\n").decode()
    compile(source, "config.py", "exec")

    assert not _contains_credential_assignment("config.py", source.encode())


def test_late_lambda_default_rebound_preserves_earlier_pairing() -> None:
    source = _seed(
        "tok",
        "en, *rest = *([runtime_token] if enabled else []), ",
        '*(["12345678"] if enabled else [runtime_token, "12345678"]), ',
        "(lambda value=(enabled := False): value)\n",
    ).decode()
    for enabled in (True, False):
        namespace = {"enabled": enabled, "runtime_token": "runtime-token"}
        exec(source, namespace)
        assert namespace["token"] == "runtime-token"

    assert not _contains_credential_assignment("config.py", source.encode())


def test_rebound_in_conditional_test_applies_before_selected_branch() -> None:
    source = _seed(
        "head, tok",
        "en, *rest = *([runtime_head] if enabled else []), ",
        "*([runtime_a, runtime_b] if (enabled := False) else ",
        '([runtime_c, runtime_d] if enabled else ["12345678", runtime_token]))\n',
    ).decode()
    namespace = {
        "enabled": True,
        "runtime_head": "head",
        "runtime_a": "a",
        "runtime_b": "b",
        "runtime_c": "c",
        "runtime_d": "d",
        "runtime_token": "runtime-token",
    }
    exec(source, namespace)
    assert namespace["token"] == "12345678"

    assert _contains_credential_assignment("config.py", source.encode())


def test_deep_conditional_credential_expression_does_not_recurse() -> None:
    expression = "runtime_value" + " if enabled else runtime_value" * 1_200
    source = _seed("tok", f"en = {expression}\n").decode()
    compile(source, "config.py", "exec")

    assert not _contains_credential_assignment("config.py", source.encode())
