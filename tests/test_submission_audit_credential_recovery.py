import pytest
from scripts.audit_submission import _contains_credential_assignment


def test_credential_suffix_is_scanned_for_literals() -> None:
    workflow = b"REGISTRY_CREDENTIAL: ya29.literal-registry-access-token\n"

    assert _contains_credential_assignment("deploy.yml", workflow)


def test_github_credential_expression_remains_symbolic() -> None:
    workflow = b"REGISTRY_CREDENTIAL: ${{ steps.gcp-auth.outputs.access_token }}\n"

    assert not _contains_credential_assignment("deploy.yml", workflow)


def test_github_format_expression_remains_symbolic() -> None:
    workflow = (
        b"REGISTRY_CREDENTIAL: "
        b"${{ format('{0}', steps.gcp-auth.outputs.access_token) }}\n"
    )

    assert not _contains_credential_assignment("deploy.yml", workflow)


def test_parser_failure_recovers_tuple_credential_assignment() -> None:
    predicate = "not " * 10_000 + "False"
    source = (
        f'token, marker = "12345678", runtime_marker\nprobe = {predicate}\n'
    ).encode()

    assert _contains_credential_assignment("config.py", source)


def test_parser_failure_recovers_indented_tuple_credential_assignment() -> None:
    predicate = "not " * 10_000 + "False"
    source = (
        'def configure():\n    token, marker = "12345678", runtime_marker\n'
        f"probe = {predicate}\n"
    ).encode()

    assert _contains_credential_assignment("config.py", source)


def test_parser_failure_ignores_assignments_inside_multiline_strings() -> None:
    predicate = "not " * 10_000 + "False"
    source = (
        'example = """\ntoken, marker = "12345678", runtime_marker\n"""\n'
        f"probe = {predicate}\n"
    ).encode()

    assert not _contains_credential_assignment("config.py", source)


def test_parser_failure_ignores_assignments_inside_multiline_f_strings() -> None:
    predicate = "not " * 10_000 + "False"
    source = (
        'example = f"""\ntoken, marker = "12345678", runtime_marker\n"""\n'
        f"probe = {predicate}\n"
    ).encode()

    assert not _contains_credential_assignment("config.py", source)


def test_invalid_dedent_does_not_expose_later_multiline_string() -> None:
    source = (
        b"if True:\n    if True:\n        pass\n      invalid_dedent = 1\n"
        b'documentation = """\n'
        b'token, marker = "12345678", runtime_marker\n'
        b'"""\n'
    )

    assert not _contains_credential_assignment("config.py", source)


def test_closing_string_line_recovers_following_tuple_assignment() -> None:
    predicate = "not " * 10_000 + "False"
    source = (
        'def configure():\n    documentation = """\n    example text\n'
        '    """; token, marker = "12345678", runtime_marker\n'
        f"probe = {predicate}\n"
    ).encode()

    assert _contains_credential_assignment("config.py", source)


@pytest.mark.parametrize(
    "prefix",
    [
        'documentation = """example"""; ',
        'documentation = """\nexample\n""".strip(); ',
    ],
)
def test_parser_failure_recovers_tuple_after_string_statement(prefix: str) -> None:
    predicate = "not " * 10_000 + "False"
    source = (
        prefix
        + 'token, marker = "12345678", runtime_marker\n'
        + f"probe = {predicate}\n"
    ).encode()

    assert _contains_credential_assignment("config.py", source)


def test_fstring_expression_delimiter_does_not_expose_string_body() -> None:
    predicate = "not " * 10_000 + "False"
    source = (
        'documentation = f"""{\'"""\'}\n'
        'token, marker = "12345678", runtime_marker\n'
        '"""\n'
        f"probe = {predicate}\n"
    ).encode()

    assert not _contains_credential_assignment("config.py", source)
