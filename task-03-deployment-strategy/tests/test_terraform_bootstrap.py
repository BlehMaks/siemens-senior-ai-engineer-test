import re
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = TASK_ROOT / "terraform" / "bootstrap"
IDENTITY = TASK_ROOT / "terraform" / "modules" / "identity"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_c03_expected_files_exist() -> None:
    expected = {
        BOOTSTRAP / "README.md",
        BOOTSTRAP / "locals.tf",
        BOOTSTRAP / "main.tf",
        BOOTSTRAP / "outputs.tf",
        BOOTSTRAP / "terraform.tfvars.example",
        BOOTSTRAP / "variables.tf",
        BOOTSTRAP / "versions.tf",
        IDENTITY / "main.tf",
        IDENTITY / "outputs.tf",
        IDENTITY / "variables.tf",
    }

    assert {path for path in expected if path.exists()} == expected


def test_versions_are_pinned_and_bootstrap_stays_backend_free() -> None:
    versions = read(BOOTSTRAP / "versions.tf")

    assert 'required_version = "~> 1.9.0"' in versions
    assert 'source  = "hashicorp/google"' in versions
    assert 'version = "~> 6.47.0"' in versions
    assert 'source  = "hashicorp/google-beta"' in versions
    assert 'backend "' not in versions


def test_bootstrap_enables_only_c03_foundational_services() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")

    for service in {
        "cloudresourcemanager.googleapis.com",
        "iam.googleapis.com",
        "iamcredentials.googleapis.com",
        "serviceusage.googleapis.com",
        "sts.googleapis.com",
        "storage.googleapis.com",
    }:
        assert f'"{service}"' in locals_tf

    assert "artifactregistry.googleapis.com" not in locals_tf
    assert "firestore.googleapis.com" not in locals_tf
    assert "run.googleapis.com" not in locals_tf


def test_state_bucket_is_private_versioned_and_not_force_destroyed() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")

    assert "uniform_bucket_level_access = true" in main_tf
    assert 'public_access_prevention    = "enforced"' in main_tf
    assert "force_destroy               = false" in main_tf
    assert re.search(r"versioning\s*\{\s*enabled = true\s*\}", main_tf)


def test_workload_inventory_and_impersonation_chain_are_explicit() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")
    main_tf = read(BOOTSTRAP / "main.tf")

    for name in ("api", "worker", "tasks", "deployer", "ci"):
        assert f"{name} =" in locals_tf

    assert 'serviceAccount:${module.identity["ci"].email}' in main_tf
    assert '"principalSet://iam.googleapis.com/%s/attribute.repository/%s"' in locals_tf


def test_provider_condition_is_repo_and_branch_scoped() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")

    assert 'attribute.repository == \\"${var.github_repository}\\"' in main_tf
    assert 'attribute.ref == \\"refs/heads/${var.github_branch}\\"' in main_tf
    assert "token.actions.githubusercontent.com" in main_tf


def test_no_keys_or_wildcard_principals_are_defined() -> None:
    role_assignments = "\n".join(
        read(path)
        for path in [
            BOOTSTRAP / "locals.tf",
            BOOTSTRAP / "main.tf",
            IDENTITY / "main.tf",
        ]
    )
    terraform_sources = "\n".join(
        read(path) for path in sorted(TASK_ROOT.glob("terraform/**/*.tf"))
    )

    assert "google_service_account_key" not in terraform_sources
    assert 'role    = "roles/owner"' not in role_assignments.lower()
    assert 'role    = "roles/editor"' not in role_assignments.lower()
    assert "allUsers" not in terraform_sources
    assert "allAuthenticatedUsers" not in terraform_sources
    assert "principalSet://iam.googleapis.com/*" not in terraform_sources
    assert 'member  = "*"' not in terraform_sources


def test_deployer_project_roles_match_reviewed_allowlist() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")

    expected_roles = {
        "roles/iam.serviceAccountAdmin",
        "roles/iam.workloadIdentityPoolAdmin",
        "roles/resourcemanager.projectIamAdmin",
        "roles/serviceusage.serviceUsageAdmin",
        "roles/storage.admin",
    }

    for role in expected_roles:
        assert f'"{role}"' in locals_tf

    role_lines = {
        line.strip().strip('",')
        for line in locals_tf.splitlines()
        if line.strip().startswith('"roles/')
    }
    assert role_lines == expected_roles


def test_example_tfvars_are_secret_free_and_realistic() -> None:
    example = read(BOOTSTRAP / "terraform.tfvars.example")

    assert "example-assignment-dev" in example
    assert "siemens-senior-ai-engineer-test" in example
    assert "secret" not in example.lower()
    assert "token" not in example.lower()
    assert "private_key" not in example.lower()
    assert "client_secret" not in example.lower()
