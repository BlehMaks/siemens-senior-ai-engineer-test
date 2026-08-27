from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.audit_submission import audit_repository


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def seed(*parts: str) -> bytes:
    return "".join(parts).encode()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitignore").write_text(".local/\ninput/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Safe public file.\n", encoding="utf-8")
    git(tmp_path, "add", ".gitignore", "README.md")
    return tmp_path


def test_clean_index_passes(repository: Path) -> None:
    assert audit_repository(repository) == []


@pytest.mark.parametrize(
    ("path", "content", "expected"),
    [
        (".local/plan.md", b"private\n", "forbidden path"),
        (".env.production", b"SAFE_VALUE=1\n", "state filename"),
        ("infra/prod.tfstate.1700000000", b"{}\n", "state filename"),
        (
            "config.py",
            seed("OPENROUTER_API_", 'KEY = "not-a-real-key"\n'),
            "OpenRouter",
        ),
        (
            "secret.txt",
            seed("-----BEGIN ", "PRIVATE KEY-----\n"),
            "private key",
        ),
        (
            "encrypted-key.txt",
            seed("-----BEGIN ENCRYPTED ", "PRIVATE KEY-----\n"),
            "private key",
        ),
        (
            "config.yaml",
            seed("api_", "key: abcdefghijklmnop\n"),
            "credential",
        ),
        (
            "runtime.env",
            seed("API_", "KEY=abcdefghijklmnop\n"),
            "credential",
        ),
        (
            "punctuation.env",
            seed("pass", 'word = "P@ssw0rd!VeryLong"\n'),
            "credential",
        ),
        (
            "config.json",
            seed('{"api_', 'key": "abcdefghijklmnop"}\n'),
            "credential",
        ),
        (
            "dsa-key.txt",
            seed("-----BEGIN DSA ", "PRIVATE KEY-----\n"),
            "private key",
        ),
        (
            "notes.md",
            seed("/Us", "ers/example/private/input.csv\n"),
            "absolute user path",
        ),
        (
            "linux-notes.md",
            seed("/ho", "me/alice/private/input.csv\n"),
            "absolute user path",
        ),
        (
            "windows-notes.md",
            seed("C:\\Us", "ers\\Alice\\private\\input.csv\n"),
            "absolute user path",
        ),
        (
            "root-notes.md",
            seed("/ro", "ot/private/input.csv\n"),
            "absolute user path",
        ),
        (
            "windows-lowercase.md",
            seed("c:\\us", "ers\\alice\\private\\input.csv\n"),
            "absolute user path",
        ),
        (".ENV.PRODUCTION", b"SAFE_VALUE=1\n", "state filename"),
        ("infra/prod.TFSTATE.BACKUP", b"{}\n", "state filename"),
    ],
)
def test_forbidden_staged_artifact_fails(
    repository: Path, path: str, content: bytes, expected: str
) -> None:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    git(repository, "add", "--force", path)

    assert any(expected in finding for finding in audit_repository(repository))


def test_nonignored_untracked_file_fails(repository: Path) -> None:
    (repository / "orphan.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert audit_repository(repository) == ["untracked public candidate: orphan.py"]


def test_ignored_local_files_do_not_enter_audit(repository: Path) -> None:
    local_file = repository / "input" / "private.csv"
    local_file.parent.mkdir()
    local_file.write_text("private data\n", encoding="utf-8")

    assert audit_repository(repository) == []


def test_tracked_symlink_fails(repository: Path) -> None:
    link = repository / "public-data.csv"
    link.symlink_to("../../.local/private.csv")
    git(repository, "add", link.name)

    assert audit_repository(repository) == ["tracked symlink: public-data.csv"]


def test_http_url_is_not_a_machine_path(repository: Path) -> None:
    link = repository / "links.md"
    link.write_text("https://example.test/home/alice/report\n", encoding="utf-8")
    git(repository, "add", link.name)

    assert audit_repository(repository) == []


@pytest.mark.parametrize(
    "content",
    [
        'signing_secret = EnvPepperProvider("AGENT_API_TASK_SIGNING_HMAC").pepper()\n',
        "secret = var.api_key_pepper_secret_id\n",
        'secret = base64.urlsafe_b64encode(b"s" * 32).decode()\n',
    ],
)
def test_symbolic_credential_expressions_are_not_secret_literals(
    repository: Path, content: str
) -> None:
    target = repository / "config.py"
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert audit_repository(repository) == []


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("runtime.env", seed("api_", "key=abcdefghijklmnop\n").decode()),
        (
            "runtime.env",
            seed("export service_", "token=p@ssw0rd!VeryLong\n").decode(),
        ),
        ("runtime.env", seed("# TOK", "EN=abcdefghijklmnop\n").decode()),
        ("config.yaml", seed("tok", "en: eyJhbGciOi!abc.def\n").decode()),
        ("config.yaml", seed("tok", "en: |\n  abcdefghijklmnop\n").decode()),
        ("settings.py", seed("API_", 'KEY = b"abcdefghijklmnop"\n').decode()),
        ("settings.py", seed("TOK", 'EN = """abcdefghijklmnop"""\n').decode()),
        (
            "settings.toml",
            seed("tok", 'en = """abcdefghijklmnop"""\n').decode(),
        ),
    ],
)
def test_additional_literal_credential_forms_fail(
    repository: Path, path: str, content: str
) -> None:
    target = repository / path
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert any("credential assignment" in item for item in audit_repository(repository))


def test_terraform_interpolation_is_not_a_secret_literal(repository: Path) -> None:
    target = repository / "main.tf"
    target.write_text(
        seed("sec", 'ret = "${var.api_key_pepper_secret_id}"\n').decode(),
        encoding="utf-8",
    )
    git(repository, "add", target.name)

    assert audit_repository(repository) == []


@pytest.mark.parametrize(
    ("path", "content"),
    [
        (
            "config.yaml",
            seed("credentials:\n  - tok", "en: abcdefghijklmnop\n").decode(),
        ),
        ("settings.toml", seed("service.tok", 'en = "abcdefghijklmnop"\n').decode()),
        ("config.json", seed('{"clientSec', 'ret": "abcdefghijklmnop"}\n').decode()),
        ("config.json", seed('{"tok', 'en": "abc\\"defghijklmnop"}\n').decode()),
    ],
)
def test_structured_credential_literals_fail(
    repository: Path, path: str, content: str
) -> None:
    target = repository / path
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert any("credential assignment" in item for item in audit_repository(repository))


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("settings.py", seed("tok", 'en = os.environ["SERVICE_TOKEN"]\n').decode()),
        (
            "settings.py",
            seed(
                "sec", 'ret = f"projects/{project_id}/secrets/{secret_id}"\n'
            ).decode(),
        ),
        (
            "main.tf",
            seed(
                "sec", 'ret = "projects/${var.project_id}/secrets/${var.secret_id}"\n'
            ).decode(),
        ),
        ("runtime.sh", seed("tok", "en=`gcloud auth print-access-token`\n").decode()),
    ],
)
def test_composite_symbolic_credential_values_pass(
    repository: Path, path: str, content: str
) -> None:
    target = repository / path
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert audit_repository(repository) == []


def test_later_credential_on_same_line_is_not_hidden_by_symbolic_value(
    repository: Path,
) -> None:
    target = repository / "config.json"
    target.write_text(
        seed(
            '{"tok',
            'en": "${RUNTIME_TOKEN}", "clientSec',
            'ret": "abcdefghijklmnop"}\n',
        ).decode(),
        encoding="utf-8",
    )
    git(repository, "add", target.name)

    assert any("credential assignment" in item for item in audit_repository(repository))


@pytest.mark.parametrize(
    ("path", "content"),
    [
        (
            "config.json",
            seed('{"pass', 'word": "Correct{Horse}Battery"}\n').decode(),
        ),
        ("runtime.env", seed("PASS", "WORD=Correct{Horse}Battery\n").decode()),
    ],
)
def test_braces_in_literal_credentials_do_not_bypass_audit(
    repository: Path, path: str, content: str
) -> None:
    target = repository / path
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert any("credential assignment" in item for item in audit_repository(repository))


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("runtime.sh", "token=$TOKEN; timeout=30\n"),
        ("runtime.sh", 'token="Bearer-$TOKEN"\n'),
        ("settings.py", "token = config.SERVICE_TOKEN\n"),
        ("main.tf", "secret = each.value.secret_id\n"),
        (
            "config.yaml",
            "credentials:\n"
            "  - token: |\n"
            "      short\n"
            "    description: this-is-a-long-nonsecret-description\n",
        ),
    ],
)
def test_additional_symbolic_and_structural_values_pass(
    repository: Path, path: str, content: str
) -> None:
    target = repository / path
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert audit_repository(repository) == []


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("settings.py", seed("pass", 'word = "Hardcoded$Secret123"\n').decode()),
        ("runtime.sh", seed("pass", "word='Hardcoded$Secret123'\n").decode()),
        ("main.tf", seed("pass", 'word = "Hardcoded$Secret123"\n').decode()),
    ],
)
def test_literal_dollar_credentials_do_not_bypass_audit(
    repository: Path, path: str, content: str
) -> None:
    target = repository / path
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert any("credential assignment" in item for item in audit_repository(repository))


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("settings.py", seed("pass", 'word = "$Secret123"\n').decode()),
        ("runtime.sh", seed("pass", "word='$Secret123'\n").decode()),
        ("main.tf", seed("pass", 'word = "$Secret123"\n').decode()),
    ],
)
def test_pure_dollar_credentials_follow_language_quoting_rules(
    repository: Path, path: str, content: str
) -> None:
    target = repository / path
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert any("credential assignment" in item for item in audit_repository(repository))


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("settings.py", seed("pass", 'word = "${Secret123}"\n').decode()),
        ("runtime.sh", seed("pass", "word='${Secret123}'\n").decode()),
        ("main.tf", seed("pass", 'word = "$${Secret123}"\n').decode()),
    ],
)
def test_braced_dollar_credentials_follow_language_escaping_rules(
    repository: Path, path: str, content: str
) -> None:
    target = repository / path
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert any("credential assignment" in item for item in audit_repository(repository))


@pytest.mark.parametrize(
    "content",
    [
        seed("pass", 'word="\\$Secret123"\n').decode(),
        seed("pass", 'word="\\${Secret123}"\n').decode(),
    ],
)
def test_escaped_shell_dollars_remain_literal(repository: Path, content: str) -> None:
    target = repository / "runtime.sh"
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert any("credential assignment" in item for item in audit_repository(repository))


@pytest.mark.parametrize(
    "content",
    [
        seed("tok", 'en="\\\\$RUNTIME_TOKEN"\n').decode(),
        seed("tok", 'en="\\\\${RUNTIME_TOKEN}"\n').decode(),
    ],
)
def test_even_shell_backslashes_preserve_interpolation(
    repository: Path, content: str
) -> None:
    target = repository / "runtime.sh"
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert audit_repository(repository) == []


def test_shell_ansi_c_quoted_dollar_is_literal(repository: Path) -> None:
    target = repository / "runtime.sh"
    target.write_text(
        seed("pass", "word=$'Hardcoded$Secret123'\n").decode(),
        encoding="utf-8",
    )
    git(repository, "add", target.name)

    assert any("credential assignment" in item for item in audit_repository(repository))


@pytest.mark.parametrize(
    "value",
    [
        '"Bearer-$1"',
        '"Bearer-$?"',
        '"Bearer-$(gcloud auth print-access-token)"',
        '"Bearer-$((TOKEN_OFFSET + 1))"',
        '"Bearer-`gcloud auth print-access-token`"',
    ],
)
def test_shell_expansion_classes_are_symbolic(repository: Path, value: str) -> None:
    target = repository / "runtime.sh"
    target.write_text(seed("tok", f"en={value}\n").decode(), encoding="utf-8")
    git(repository, "add", target.name)

    assert audit_repository(repository) == []


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("config.json", seed('{"tok', 'en": "${RUNTIME_TOKEN}"}\n').decode()),
        ("config.yaml", seed("tok", "en: ${RUNTIME_TOKEN}\n").decode()),
        ("settings.toml", seed("tok", 'en = "${RUNTIME_TOKEN}"\n').decode()),
        ("runtime.env", seed("TOK", "EN=${RUNTIME_TOKEN}\n").decode()),
    ],
)
def test_structured_config_interpolation_is_symbolic(
    repository: Path, path: str, content: str
) -> None:
    target = repository / path
    target.write_text(content, encoding="utf-8")
    git(repository, "add", target.name)

    assert audit_repository(repository) == []


@pytest.mark.parametrize("indicator", ["|", ">-"])
def test_yaml_block_interpolation_is_symbolic(repository: Path, indicator: str) -> None:
    target = repository / "config.yaml"
    target.write_text(
        seed("tok", f"en: {indicator}\n  ${{RUNTIME_TOKEN}}\n").decode(),
        encoding="utf-8",
    )
    git(repository, "add", target.name)

    assert audit_repository(repository) == []


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("bin/deploy", '#!/usr/bin/env bash\ntoken="Bearer-$TOKEN"\n'),
        (
            "main.tf.json",
            seed(
                '{"sec', 'ret": "projects/${var.project}/secrets/${var.secret}"}\n'
            ).decode(),
        ),
        (
            "settings.py",
            'secret = "projects/{project}/secrets/{name}".format('
            "project=project_id, name=secret_id)\n",
        ),
    ],
)
def test_additional_language_specific_symbolic_values_pass(
    repository: Path, path: str, content: str
) -> None:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repository, "add", path)

    assert audit_repository(repository) == []
