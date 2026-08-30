from scripts.audit_submission import _contains_credential_assignment


def test_credential_suffix_is_scanned_for_literals() -> None:
    workflow = b"REGISTRY_CREDENTIAL: ya29.literal-registry-access-token\n"

    assert _contains_credential_assignment("deploy.yml", workflow)


def test_github_credential_expression_remains_symbolic() -> None:
    workflow = (
        b"REGISTRY_CREDENTIAL: ${{ steps.gcp-auth.outputs.access_token }}\n"
    )

    assert not _contains_credential_assignment("deploy.yml", workflow)


def test_parser_failure_recovers_tuple_credential_assignment() -> None:
    predicate = "not " * 10_000 + "False"
    source = (
        'token, marker = "12345678", runtime_marker\n'
        f"probe = {predicate}\n"
    ).encode()

    assert _contains_credential_assignment("config.py", source)
