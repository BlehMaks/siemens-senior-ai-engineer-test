import re
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
MODULE = TASK_ROOT / "terraform" / "modules" / "managed_services"
DEV = TASK_ROOT / "terraform" / "environments" / "dev"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_c04_expected_files_exist() -> None:
    expected = {
        MODULE / "main.tf",
        MODULE / "outputs.tf",
        MODULE / "tests" / "c04_managed_services.tftest.hcl",
        MODULE / "variables.tf",
        MODULE / "versions.tf",
        DEV / ".terraform.lock.hcl",
        DEV / "README.md",
        DEV / "cost-review.md",
        DEV / "destroy-review.md",
        DEV / "main.tf",
        DEV / "terraform.tfvars.example",
        DEV / "variables.tf",
        DEV / "versions.tf",
    }

    assert {path for path in expected if path.exists()} == expected


def test_dev_provider_selections_are_locked() -> None:
    lock = read(DEV / ".terraform.lock.hcl")

    assert 'provider "registry.terraform.io/hashicorp/google"' in lock
    assert 'provider "registry.terraform.io/hashicorp/google-beta"' in lock
    assert lock.count('version     = "6.47.0"') == 2
    assert lock.count('constraints = "~> 6.47.0"') == 2


def test_secrets_are_regional_empty_containers() -> None:
    main = read(MODULE / "main.tf")

    assert 'resource "google_secret_manager_secret" "managed"' in main
    assert 'resource "google_secret_manager_secret_version"' not in main
    assert "secret_data" not in main
    assert "payload" not in main
    assert "deletion_protection = true" in main
    assert "user_managed" in main
    assert "auto {}" not in main


def test_secret_and_artifact_access_is_resource_specific() -> None:
    main = read(MODULE / "main.tf")
    assigned_roles = set(re.findall(r'role\s*=\s*"([^"]+)"', main))

    assert assigned_roles == {
        "roles/artifactregistry.writer",
        "roles/secretmanager.secretAccessor",
    }
    assert 'resource "google_project_iam_member"' not in main
    assert (
        'resource "google_secret_manager_secret_iam_member" "api_pepper_reader"' in main
    )
    assert (
        'secret_id = google_secret_manager_secret.managed["api_key_pepper"].secret_id'
        in main
    )
    assert "local.worker_member" in main
    assert "member    = each.value" in main
    assert (
        'resource "google_secret_manager_secret_iam_member" "task_hmac_reader"' in main
    )
    assert "for_each = local.workload_members" in main
    assert "repository = google_artifact_registry_repository.containers.name" in main
    assert 'resource "google_firestore_index" "sessions"' in main
    assert 'resource "google_firestore_index" "runs"' in main
    assert 'resource "google_firestore_index" "run_events"' in main
    assert "allUsers" not in main
    assert "allAuthenticatedUsers" not in main
    assert 'member = "*"' not in main


def test_data_lifecycle_and_alerts_fail_safe() -> None:
    main = read(MODULE / "main.tf")
    variables = read(MODULE / "variables.tf")

    assert (
        'point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"' in main
    )
    assert re.search(r"location\s*=\s*var\.region", main)
    assert "length(var.budget_notification_emails) > 0" in main
    assert "disable_default_iam_recipients = true" in main
    assert "monitoring_notification_channels" in main
    assert "length(var.labels) <= 61" in variables
    assert "length(toset([var.secret_ids.api_key_pepper" in variables
    assert variables.count("@${var.project_id}.iam.gserviceaccount.com") == 3
    assert "all workload identities must be distinct" in variables


def test_operator_docs_state_cost_and_destroy_limits() -> None:
    readme = read(DEV / "README.md")
    cost_review = read(DEV / "cost-review.md")
    destroy_review = read(DEV / "destroy-review.md")

    assert "secret payloads and versions" in readme
    assert "explicit recipient" in readme
    assert "alert, not an enforcement boundary" in cost_review
    assert "DELETE_PROTECTION_ENABLED" not in destroy_review
    assert "delete protection is enabled" in destroy_review
    assert "ABANDON" in destroy_review
