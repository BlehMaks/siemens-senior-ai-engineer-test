import re
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = TASK_ROOT / "terraform" / "bootstrap"
STATE_BUCKET = TASK_ROOT / "terraform" / "state_bucket"
IDENTITY = TASK_ROOT / "terraform" / "modules" / "identity"
RUN_SERVICES = TASK_ROOT / "terraform" / "modules" / "run_services"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_c03_expected_files_exist() -> None:
    expected = {
        BOOTSTRAP / ".terraform.lock.hcl",
        BOOTSTRAP / "README.md",
        BOOTSTRAP / "github.tf",
        BOOTSTRAP / "locals.tf",
        BOOTSTRAP / "main.tf",
        BOOTSTRAP / "outputs.tf",
        BOOTSTRAP / "terraform.tfvars.example",
        BOOTSTRAP / "secret_versions.tf",
        BOOTSTRAP / "variables.tf",
        BOOTSTRAP / "versions.tf",
        IDENTITY / "main.tf",
        IDENTITY / "outputs.tf",
        IDENTITY / "variables.tf",
        STATE_BUCKET / ".terraform.lock.hcl",
        STATE_BUCKET / "main.tf",
        STATE_BUCKET / "outputs.tf",
        STATE_BUCKET / "variables.tf",
        STATE_BUCKET / "versions.tf",
    }

    assert {path for path in expected if path.exists()} == expected


def test_versions_are_pinned_and_bootstrap_uses_migratable_gcs_state() -> None:
    versions = read(BOOTSTRAP / "versions.tf")

    assert 'required_version = "~> 1.9.0"' in versions
    assert 'source  = "hashicorp/google"' in versions
    assert 'version = "~> 6.47.0"' in versions
    assert 'source  = "hashicorp/google-beta"' in versions
    assert 'source  = "integrations/github"' in versions
    assert 'version = "~> 6.13.0"' in versions
    assert 'backend "gcs" {}' in versions
    assert "bucket" not in versions
    assert "prefix" not in versions


def test_provider_selections_are_locked_for_supported_platforms() -> None:
    lock = read(BOOTSTRAP / ".terraform.lock.hcl")

    assert 'provider "registry.terraform.io/hashicorp/google"' in lock
    assert 'provider "registry.terraform.io/hashicorp/google-beta"' in lock
    assert 'provider "registry.terraform.io/integrations/github"' in lock
    assert lock.count('version     = "6.47.0"') == 2
    assert lock.count('constraints = "~> 6.47.0"') == 2
    assert 'version     = "6.13.0"' in lock
    assert 'constraints = "~> 6.13.0"' in lock
    assert lock.count("h1:") >= 6


def test_bootstrap_enables_only_foundational_and_protected_services() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")

    for service in {
        "cloudresourcemanager.googleapis.com",
        "cloudtasks.googleapis.com",
        "iam.googleapis.com",
        "iamcredentials.googleapis.com",
        "secretmanager.googleapis.com",
        "serviceusage.googleapis.com",
        "sts.googleapis.com",
        "storage.googleapis.com",
        "run.googleapis.com",
    }:
        assert f'"{service}"' in locals_tf

    assert "artifactregistry.googleapis.com" not in locals_tf
    assert "firestore.googleapis.com" not in locals_tf


def test_state_bucket_is_private_versioned_and_not_force_destroyed() -> None:
    main_tf = read(STATE_BUCKET / "main.tf")
    bootstrap_main = read(BOOTSTRAP / "main.tf")

    assert "for_each = local.state_buckets" in main_tf
    assert "application = var.application_state_bucket_name" in main_tf
    assert "bootstrap   = var.bootstrap_state_bucket_name" in main_tf
    assert "uniform_bucket_level_access = true" in main_tf
    assert 'public_access_prevention    = "enforced"' in main_tf
    assert "force_destroy               = false" in main_tf
    assert re.search(r"versioning\s*\{\s*enabled = true\s*\}", main_tf)
    assert 'resource "google_storage_bucket" "terraform_state"' not in bootstrap_main


def test_workload_inventory_and_direct_federation_are_explicit() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")
    main_tf = read(BOOTSTRAP / "main.tf")

    for name in ("api", "worker", "tasks", "deployer"):
        assert f"{name} =" in locals_tf

    assert "ci =" not in locals_tf
    deployer = main_tf.split('module "deployer_identity"', maxsplit=1)[1].split(
        'resource "', maxsplit=1
    )[0]
    assert "workload_identity_members" in deployer
    assert "{ github = local.github_principal }" in deployer
    assert "token_creator_members        = {}" in deployer
    assert (
        '"principalSet://iam.googleapis.com/%s/attribute.repository_id/%s"' in locals_tf
    )


def test_provider_condition_uses_immutable_id_repo_and_branch_scope() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")
    variables_tf = read(BOOTSTRAP / "variables.tf")
    github_tf = read(BOOTSTRAP / "github.tf")

    assert '"attribute.repository_id" = "assertion.repository_id"' in main_tf
    assert (
        'attribute.repository_id == \\"${data.github_repository.target[0].repo_id}\\"'
        in main_tf
    )
    assert 'attribute.repository == \\"${var.github_repository}\\"' in main_tf
    assert 'attribute.ref == \\"refs/heads/${var.github_branch}\\"' in main_tf
    assert 'data "github_repository" "target"' in github_tf
    assert "github_repository_id" not in variables_tf
    assert 'variable "github_reviewer"' in variables_tf
    assert "token.actions.githubusercontent.com" in main_tf


def test_federated_members_use_stable_plan_time_keys() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")
    identity_variables = read(IDENTITY / "variables.tf")

    assert identity_variables.count("type        = map(string)") >= 4
    assert "{ github = local.github_principal }" in main_tf
    assert "? [local.github_principal]" not in main_tf
    assert 'module "deployer_identity"' in main_tf
    assert 'module.identity["ci"]' not in main_tf


def test_no_keys_or_wildcard_principals_are_defined() -> None:
    role_assignments = "\n".join(
        read(path)
        for path in [
            BOOTSTRAP / "locals.tf",
            BOOTSTRAP / "main.tf",
            IDENTITY / "main.tf",
            RUN_SERVICES / "main.tf",
        ]
    )
    terraform_sources = "\n".join(
        read(path) for path in sorted(TASK_ROOT.glob("terraform/**/*.tf"))
    )

    assert "google_service_account_key" not in terraform_sources
    for role in ("owner", "editor", "viewer"):
        assert f'role    = "roles/{role}"' not in role_assignments.lower()
    assert role_assignments.count('member   = "allUsers"') == 1
    assert "allAuthenticatedUsers" not in role_assignments
    assert "principalSet://iam.googleapis.com/*" not in terraform_sources
    assert 'member  = "*"' not in terraform_sources


def test_project_roles_match_reviewed_allowlist() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")
    main_tf = read(BOOTSTRAP / "main.tf")
    identity_variables = read(IDENTITY / "variables.tf")

    expected_unconditional_roles = {
        "roles/artifactregistry.admin",
        "roles/logging.configWriter",
        "roles/monitoring.notificationChannelEditor",
        "roles/serviceusage.serviceUsageAdmin",
    }

    for role in expected_unconditional_roles:
        assert f'"{role}"' in locals_tf

    role_lines = set(re.findall(r'"(roles/[A-Za-z0-9_.]+)"', locals_tf))
    assert role_lines == expected_unconditional_roles
    assert locals_tf.count("project_roles = []") == 3
    assert "project_roles = local.deployer_project_roles" in locals_tf
    assert 'role    = "roles/datastore.user"' in main_tf
    assert 'role    = "roles/datastore.indexAdmin"' in main_tf
    assert main_tf.count("resource.name ==") >= 2
    assert "local.firestore_database_name" in main_tf
    assert '["roles/owner", "roles/editor", "roles/viewer"]' in identity_variables
    assert "role == trimspace(role)" in identity_variables
    assert 'regex("^roles/[A-Za-z0-9_.]+$", role)' in identity_variables


def test_cloud_run_iam_is_bootstrap_owned_and_service_scoped() -> None:
    locals_tf = read(BOOTSTRAP / "locals.tf")
    main_tf = read(BOOTSTRAP / "main.tf")
    run_services_tf = read(RUN_SERVICES / "main.tf")

    assert '"run.services.setIamPolicy"' not in locals_tf
    assert '"run.services.getIamPolicy"' not in locals_tf
    assert "deployer_cloud_run_iam" not in main_tf
    assert (
        'resource "google_cloud_run_v2_service_iam_binding" "worker_invoker"' in main_tf
    )
    assert (
        'resource "google_cloud_run_v2_service_iam_member" "api_public_invoker"'
        in main_tf
    )
    assert "count = var.enable_runtime_policy ? 1 : 0" in main_tf
    assert 'name     = "${var.system_code}-${var.environment}-worker"' in main_tf
    assert 'name     = "${var.system_code}-${var.environment}-api"' in main_tf
    assert "google_cloud_run_v2_service_iam_" not in run_services_tf


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
        "datastore.databases.create",
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
    assert "datastore.databases.delete" not in permission_lines
    assert "run.services.sshRoot" not in permission_lines
    assert not any(
        permission.endswith(".setIamPolicy") for permission in permission_lines
    )
    assert "roles/cloudtasks.queueAdmin" not in locals_tf


def test_deployer_budget_role_is_scoped_to_the_linked_billing_account() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")
    binding = main_tf.split(
        'resource "google_billing_account_iam_member" "deployer_budget_manager"',
        maxsplit=1,
    )[1].split('resource "', maxsplit=1)[0]

    assert 'count = var.billing_account_id == "" ? 0 : 1' in binding
    assert "billing_account_id = var.billing_account_id" in binding
    assert 'role               = "roles/billing.costsManager"' in binding
    assert (
        'member             = "serviceAccount:${module.deployer_identity.email}"'
        in binding
    )


def test_secrets_and_runtime_access_stay_in_human_bootstrap() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")
    outputs_tf = read(BOOTSTRAP / "outputs.tf")
    secret_versions_tf = read(BOOTSTRAP / "secret_versions.tf")

    assert 'resource "google_secret_manager_secret" "managed"' in main_tf
    assert 'resource "google_secret_manager_secret_version"' not in main_tf
    assert "secret_data" not in main_tf
    assert "secret_data" not in secret_versions_tf
    assert 'resource "terraform_data" "secret_version"' in secret_versions_tf
    assert "seed_secret_version.sh" in secret_versions_tf
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
    for resource_name in ("api_pepper_reader", "task_hmac_reader"):
        resource = main_tf.split(
            f'resource "google_secret_manager_secret_iam_member" "{resource_name}"',
            maxsplit=1,
        )[1].split("resource ", maxsplit=1)[0]
        assert 'for_each = toset(["api", "worker"])' in resource
    assert main_tf.count('role      = "roles/secretmanager.secretAccessor"') == 2
    assert 'output "secret_containers"' in outputs_tf
    assert 'output "secret_accessors"' in outputs_tf


def test_deployer_can_attach_only_the_two_cloud_run_identities() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")

    resource = main_tf.split(
        'resource "google_service_account_iam_member" "deployer_runtime_user"',
        maxsplit=1,
    )[1]
    assert 'toset(["api", "worker"])' in resource
    assert 'toset(["api", "tasks", "worker"])' not in resource
    assert 'role               = "roles/iam.serviceAccountUser"' in resource
    assert (
        'member             = "serviceAccount:${module.deployer_identity.email}"'
        in resource
    )


def test_queue_and_service_iam_are_owned_by_human_bootstrap() -> None:
    main_tf = read(BOOTSTRAP / "main.tf")
    run_services_tf = read(RUN_SERVICES / "main.tf")

    assert "deployer_tasks_policy_admin" not in main_tf
    assert "iam.serviceAccounts.setIamPolicy" not in main_tf
    assert "roles/iam.serviceAccountAdmin" not in main_tf
    assert (
        'resource "google_service_account_iam_member" '
        '"tasks_service_agent_token_creator"' in main_tf
    )
    assert (
        'member             = "serviceAccount:${google_project_service_identity.cloud_tasks.email}"'
        in main_tf
    )
    assert 'role               = "roles/iam.serviceAccountTokenCreator"' in main_tf
    assert "enable_runtime_policy" in main_tf
    assert main_tf.count('resource "google_cloud_tasks_queue_iam_member"') == 1
    assert 'resource "google_cloud_tasks_queue" "dispatch"' in main_tf
    assert "name     = google_cloud_tasks_queue.dispatch.name" in main_tf
    assert "local.worker_service_url" in main_tf
    assert (
        'resource "google_cloud_run_v2_service_iam_binding" "worker_invoker"' in main_tf
    )
    assert (
        'resource "google_cloud_run_v2_service_iam_member" "api_public_invoker"'
        in main_tf
    )
    assert "google_cloud_run_v2_service_iam_" not in run_services_tf


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
    assert "bucket = var.application_state_bucket_name" in resource
    assert "bootstrap_state_bucket_name" not in resource
    assert 'role   = "roles/storage.objectAdmin"' in resource
    assert 'member = "serviceAccount:${module.deployer_identity.email}"' in resource


def test_example_tfvars_are_secret_free_and_realistic() -> None:
    example = read(BOOTSTRAP / "terraform.tfvars.example")

    assert "example-assignment-dev" in example
    assert "siemens-senior-ai-engineer-test" in example
    assert "github_repository_id" not in example
    assert "token" not in example.lower()
    assert "private_key" not in example.lower()
    assert "client_secret" not in example.lower()
    assert 'api_key_pepper    = "sai-dev-api-key-pepper"' in example
    assert 'task_signing_hmac = "sai-dev-task-signing-hmac"' in example
    assert 'github_branch        = "master"' in example
    assert 'github_environment   = "gcp-dev"' in example
    assert 'github_reviewer      = "example-reviewer"' in example
    assert (
        'bootstrap_state_bucket_name   = "example-assignment-dev-sai-bootstrap-tf-state"'
        in example
    )
    assert (
        'application_state_bucket_name = "example-assignment-dev-sai-app-tf-state"'
        in example
    )


def test_github_environment_and_delivery_variables_are_terraform_managed() -> None:
    github_tf = read(BOOTSTRAP / "github.tf")
    locals_tf = read(BOOTSTRAP / "locals.tf")

    assert 'resource "github_branch_protection" "delivery"' in github_tf
    assert (
        "repository_id           = data.github_repository.target[0].node_id"
        in github_tf
    )
    assert "enforce_admins          = true" in github_tf
    assert "required_linear_history = true" in github_tf
    assert "allows_deletions        = false" in github_tf
    assert "allows_force_pushes     = false" in github_tf
    assert 'resource "github_repository_environment" "deployment"' in github_tf
    assert "reviewers {" in github_tf
    assert "custom_branch_policies = true" in github_tf
    assert (
        'resource "github_repository_environment_deployment_policy" "branch"'
        in github_tf
    )
    assert 'resource "github_actions_environment_variable" "delivery"' in github_tf
    for variable in {
        "GCP_PROJECT_ID",
        "GCP_PROJECT_NUMBER",
        "GCP_TERRAFORM_STATE_BUCKET",
        "GCP_WORKLOAD_IDENTITY_PROVIDER",
        "GCP_DEPLOYER_SERVICE_ACCOUNT",
        "GCP_API_SERVICE_ACCOUNT",
        "GCP_WORKER_SERVICE_ACCOUNT",
        "GCP_TASKS_SERVICE_ACCOUNT",
        "GCP_SECRET_IDS",
    }:
        assert variable in locals_tf
