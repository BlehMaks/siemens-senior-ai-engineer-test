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
