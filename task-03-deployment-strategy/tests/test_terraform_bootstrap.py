import re
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = TASK_ROOT / "terraform" / "bootstrap"
IDENTITY = TASK_ROOT / "terraform" / "modules" / "identity"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_c03_expected_files_exist() -> None:
    expected = {
        BOOTSTRAP / ".terraform.lock.hcl",
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


def test_provider_selections_are_locked_for_supported_platforms() -> None:
    lock = read(BOOTSTRAP / ".terraform.lock.hcl")

    assert 'provider "registry.terraform.io/hashicorp/google"' in lock
    assert 'provider "registry.terraform.io/hashicorp/google-beta"' in lock
    assert lock.count('version     = "6.47.0"') == 2
    assert lock.count('constraints = "~> 6.47.0"') == 2
    assert lock.count("h1:") >= 4


def test_bootstrap_enables_only_foundational_and_protected_services() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")

    for service in {
        "cloudresourcemanager.googleapis.com",
        "iam.googleapis.com",
        "iamcredentials.googleapis.com",
        "secretmanager.googleapis.com",
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
    assert (
        '"principalSet://iam.googleapis.com/%s/attribute.repository_id/%s"' in locals_tf
    )


def test_provider_condition_uses_immutable_id_repo_and_branch_scope() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")
    variables_tf = read(BOOTSTRAP / "variables.tf")

    assert '"attribute.repository_id" = "assertion.repository_id"' in main_tf
    assert 'attribute.repository_id == \\"${var.github_repository_id}\\"' in main_tf
    assert 'attribute.repository == \\"${var.github_repository}\\"' in main_tf
    assert 'attribute.ref == \\"refs/heads/${var.github_branch}\\"' in main_tf
    assert 'default     = ""' in variables_tf
    assert "!var.enable_github_wif" in variables_tf
    assert 'regex("^[1-9][0-9]*$", var.github_repository_id)' in variables_tf
    assert "token.actions.githubusercontent.com" in main_tf


def test_federated_members_use_stable_plan_time_keys() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")
    identity_variables = read(IDENTITY / "variables.tf")

    assert identity_variables.count("type        = map(string)") >= 4
    assert "? { github = local.github_principal }" in main_tf
    assert "? [local.github_principal]" not in main_tf
    assert 'ci = "serviceAccount:${module.identity["ci"].email}"' in main_tf


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
    for role in ("owner", "editor", "viewer"):
        assert f'role    = "roles/{role}"' not in role_assignments.lower()
    assert "allUsers" not in role_assignments
    assert "allAuthenticatedUsers" not in role_assignments
    assert "principalSet://iam.googleapis.com/*" not in terraform_sources
    assert 'member  = "*"' not in terraform_sources


def test_project_roles_match_reviewed_allowlist() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")
    identity_variables = read(IDENTITY / "variables.tf")

    expected_roles = {
        "roles/artifactregistry.admin",
        "roles/cloudtasks.queueAdmin",
        "roles/datastore.indexAdmin",
        "roles/datastore.user",
        "roles/logging.configWriter",
        "roles/monitoring.notificationChannelEditor",
        "roles/serviceusage.serviceUsageAdmin",
    }

    for role in expected_roles:
        assert f'"{role}"' in locals_tf

    role_lines = set(re.findall(r'"(roles/[A-Za-z0-9_.]+)"', locals_tf))
    assert role_lines == expected_roles
    assert locals_tf.count('project_roles = ["roles/datastore.user"]') == 2
    assert "project_roles = local.deployer_project_roles" in locals_tf
    assert '["roles/owner", "roles/editor", "roles/viewer"]' in identity_variables
    assert "role == trimspace(role)" in identity_variables
    assert 'regex("^roles/[A-Za-z0-9_.]+$", role)' in identity_variables


def test_deployer_uses_custom_project_role_without_project_iam_admin() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")
    main_tf = read(BOOTSTRAP / "main.tf")

    assert 'resource "google_project_iam_custom_role" "deployer_application"' in main_tf
    assert 'resource "google_project_iam_member" "deployer_custom_role"' in main_tf
    assert "local.deployer_project_permissions" in main_tf
    assert "roles/resourcemanager.projectIamAdmin" not in locals_tf
    assert "roles/iam.serviceAccountAdmin" not in locals_tf
    assert "roles/datastore.admin" not in locals_tf
    assert "roles/storage.admin" not in locals_tf
    assert "roles/iam.workloadIdentityPoolAdmin" not in locals_tf
    assert "roles/run.admin" not in locals_tf
    assert "roles/secretmanager.admin" not in locals_tf
    assert "datastore.entities" not in locals_tf

    expected_custom_permissions = {
        "billing.resourcebudgets.read",
        "billing.resourcebudgets.write",
        "datastore.databases.create",
        "datastore.databases.delete",
        "datastore.databases.getMetadata",
        "datastore.databases.list",
        "datastore.databases.update",
        "datastore.locations.get",
        "datastore.locations.list",
        "datastore.operations.get",
        "datastore.operations.list",
        "resourcemanager.projects.get",
        "run.operations.get",
        "run.services.create",
        "run.services.delete",
        "run.services.get",
        "run.services.getIamPolicy",
        "run.services.setIamPolicy",
        "run.services.update",
    }
    permission_block = locals_tf.split(
        "deployer_project_permissions = toset([", maxsplit=1
    )[1].split("])", maxsplit=1)[0]
    permission_lines = {
        line.strip().strip('",')
        for line in permission_block.splitlines()
        if line.strip().startswith('"') and "." in line
    }
    assert permission_lines == expected_custom_permissions
    assert "run.routes.invoke" not in permission_lines
    assert "run.services.sshRoot" not in permission_lines


def test_secrets_and_runtime_access_stay_in_human_bootstrap() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")
    outputs_tf = read(BOOTSTRAP / "outputs.tf")

    assert 'resource "google_secret_manager_secret" "managed"' in main_tf
    assert 'resource "google_secret_manager_secret_version"' not in main_tf
    assert "secret_data" not in main_tf
    assert "deletion_protection = true" in main_tf
    assert "user_managed" in main_tf
    assert (
        'resource "google_secret_manager_secret_iam_member" "api_pepper_reader"'
        in main_tf
    )
    assert (
        'resource "google_secret_manager_secret_iam_member" "task_hmac_reader"'
        in main_tf
    )
    assert main_tf.count('for_each = toset(["api", "worker"])') == 2
    assert main_tf.count('role      = "roles/secretmanager.secretAccessor"') == 2
    assert 'output "secret_containers"' in outputs_tf
    assert 'output "secret_accessors"' in outputs_tf


def test_deployer_can_attach_only_the_three_runtime_identities() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")

    resource = main_tf.split(
        'resource "google_service_account_iam_member" "deployer_runtime_user"',
        maxsplit=1,
    )[1]
    assert 'toset(["api", "tasks", "worker"])' in resource
    assert 'role               = "roles/iam.serviceAccountUser"' in resource
    assert (
        'member             = "serviceAccount:${module.deployer_identity.email}"'
        in resource
    )


def test_deployer_policy_admin_is_limited_to_tasks_identity() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")
    main_tf = read(BOOTSTRAP / "main.tf")

    resource = main_tf.split(
        'resource "google_service_account_iam_member" "deployer_tasks_policy_admin"',
        maxsplit=1,
    )[1]
    assert 'service_account_id = module.identity["tasks"].name' in resource
    assert (
        "role               = google_project_iam_custom_role.tasks_policy.name"
        in resource
    )
    assert (
        'member             = "serviceAccount:${module.deployer_identity.email}"'
        in resource
    )
    assert "roles/iam.serviceAccountAdmin" not in main_tf
    expected_policy_permissions = {
        "iam.serviceAccounts.get",
        "iam.serviceAccounts.getIamPolicy",
        "iam.serviceAccounts.setIamPolicy",
    }
    permission_block = locals_tf.split(
        "tasks_policy_permissions = toset([", maxsplit=1
    )[1].split("])", maxsplit=1)[0]
    assert expected_policy_permissions == {
        line.strip().strip('",')
        for line in permission_block.splitlines()
        if line.strip().startswith('"')
    }


def test_deployer_state_access_is_bucket_scoped() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")

    resource = main_tf.split(
        'resource "google_storage_bucket_iam_member" "deployer_state_objects"',
        maxsplit=1,
    )[1]
    resource = resource.split(
        'resource "google_service_account_iam_member" "deployer_runtime_user"',
        maxsplit=1,
    )[0]
    assert "bucket = google_storage_bucket.terraform_state.name" in resource
    assert 'role   = "roles/storage.objectAdmin"' in resource
    assert 'member = "serviceAccount:${module.deployer_identity.email}"' in resource


def test_example_tfvars_are_secret_free_and_realistic() -> None:
    example = read(BOOTSTRAP / "terraform.tfvars.example")

    assert "example-assignment-dev" in example
    assert "siemens-senior-ai-engineer-test" in example
    assert 'github_repository_id = "123456789"' in example
    assert "token" not in example.lower()
    assert "private_key" not in example.lower()
    assert "client_secret" not in example.lower()
    assert 'api_key_pepper    = "sai-dev-api-key-pepper"' in example
    assert 'task_signing_hmac = "sai-dev-task-signing-hmac"' in example
    assert 'github_branch        = "master"' in example
    assert 'github_environment   = "gcp-dev"' in example
