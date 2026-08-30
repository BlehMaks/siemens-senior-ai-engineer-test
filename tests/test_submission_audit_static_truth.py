from __future__ import annotations

from scripts.audit_submission import _contains_credential_assignment


def test_false_named_expression_stops_later_rebind_analysis() -> None:
    source = (
        "token, marker = "
        "*([runtime_token] if enabled else []), "
        "*((['12345678'] if enabled else [runtime_token, '12345678']) "
        "if ((guard := False) and (enabled := False)) else "
        "(['12345678'] if enabled else [runtime_token, '12345678']))\n"
    )

    assert not _contains_credential_assignment("config.py", source.encode())


def test_deep_static_predicate_is_evaluated_iteratively() -> None:
    predicate = "not " * 1_200 + "False"
    source = f"token, marker = runtime_token, ({predicate} and (enabled := False))\n"

    assert not _contains_credential_assignment("config.py", source.encode())


def test_nested_static_boolop_stops_unreachable_rebind_analysis() -> None:
    source = (
        "token, marker = "
        "*([runtime_token] if enabled else []), "
        "*((['12345678'] if enabled else [runtime_token, '12345678']) "
        "if (((guard := False) and (never := False)) and (enabled := False)) "
        "else (['12345678'] if enabled else [runtime_token, '12345678']))\n"
    )

    assert not _contains_credential_assignment("config.py", source.encode())


def test_parser_complexity_failure_falls_back_without_crashing() -> None:
    predicate = "not " * 10_000 + "False"
    source = f"token, marker = runtime_token, ({predicate})\n"

    assert not _contains_credential_assignment("config.py", source.encode())
