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
