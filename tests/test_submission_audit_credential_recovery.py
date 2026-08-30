import scripts.audit_submission as submission_audit
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


def test_parser_resource_failure_fails_closed() -> None:
    predicate = "not " * 10_000 + "False"
    source = f"runtime_value = {predicate}\n".encode()

    assert _contains_credential_assignment("config.py", source)


def test_fail_closed_result_returns_without_linewise_rescan(monkeypatch) -> None:
    monkeypatch.setattr(
        submission_audit,
        "_python_contains_credential_assignment",
        lambda content: True,
    )

    def exhausted_memory(path: str, content: bytes) -> bool:
        raise MemoryError("linewise scan also exhausted memory")

    monkeypatch.setattr(
        submission_audit,
        "_contains_credential_assignment_linewise",
        exhausted_memory,
    )

    assert _contains_credential_assignment("config.py", b"runtime_value = False\n")
