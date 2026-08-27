from __future__ import annotations

from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
TERRAFORM_ROOT = TASK_ROOT / "terraform"
RUN_SERVICES_MAIN = TERRAFORM_ROOT / "modules" / "run_services" / "main.tf"
RUN_SERVICES_VARIABLES = TERRAFORM_ROOT / "modules" / "run_services" / "variables.tf"
DEV_VARIABLES = TERRAFORM_ROOT / "environments" / "dev" / "variables.tf"
INGRESS_POLICY_TEST = (
    TERRAFORM_ROOT
    / "modules"
    / "ingress_policy"
    / "tests"
    / "c05_ingress_policy.tftest.hcl"
)
RUN_SERVICES_TEST = (
    TERRAFORM_ROOT
    / "modules"
    / "run_services"
    / "tests"
    / "c05_run_services.tftest.hcl"
)
DEV_MAIN = TERRAFORM_ROOT / "environments" / "dev" / "main.tf"
DEV_README = TERRAFORM_ROOT / "environments" / "dev" / "README.md"
ATTACK_PATH = TERRAFORM_ROOT / "environments" / "dev" / "c05a-attack-path.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_run_services_keeps_worker_private_and_oidc_bound() -> None:
    source = read(RUN_SERVICES_MAIN)

    assert source.count("invoker_iam_disabled = false") == 2
    assert "ingress              = var.worker_ingress" in source
    assert (
        'resource "google_cloud_run_v2_service_iam_binding" "worker_invoker"' in source
    )
    assert 'role     = "roles/run.invoker"' in source
    assert 'members  = ["serviceAccount:${var.tasks_service_account_email}"]' in source
    assert 'role     = "roles/cloudtasks.enqueuer"' in source
    assert 'member   = "serviceAccount:${var.api_service_account_email}"' in source
    assert 'role               = "roles/iam.serviceAccountTokenCreator"' in source
    assert "service_account_email = var.tasks_service_account_email" in source
    assert "audience              = google_cloud_run_v2_service.worker.uri" in source
    assert (
        'for_each = var.api_allow_unauthenticated ? toset(["baseline"]) : toset([])'
        in source
    )
    assert source.count('member   = "allUsers"') == 1
    assert "allAuthenticatedUsers" not in source


def test_run_services_uses_secret_refs_and_not_secret_payloads() -> None:
    source = read(RUN_SERVICES_MAIN)

    assert source.count('name = "AGENT_API_KEY_PEPPER"') == 2
    assert 'name = "AGENT_API_TASK_SIGNING_HMAC"' in source
    assert 'version = "latest"' in source
    assert "secret_data" not in source
    assert "payload" not in source


def test_run_services_is_digest_pinned_and_bounded() -> None:
    variables = read(RUN_SERVICES_VARIABLES)
    source = read(RUN_SERVICES_MAIN)

    assert 'regex("^sha256:[a-f0-9]{64}$"' in variables
    assert "min_instance_count = 0" in source
    assert "max_instance_count = var.api_max_instances" in source
    assert "max_instance_count = var.worker_max_instances" in source
    assert "max_dispatches_per_second = var.queue_max_dispatches_per_second" in source
    assert "max_concurrent_dispatches = var.queue_max_concurrent_dispatches" in source
    assert "max_attempts       = var.queue_max_attempts" in source
    assert "prevent_destroy = true" in source
    assert "var.queue_max_retry_seconds >= 1" in variables


def test_run_services_resolves_cloud_tasks_service_agent_from_project() -> None:
    source = read(RUN_SERVICES_MAIN)
    variables = read(RUN_SERVICES_VARIABLES)

    assert 'data "google_project" "current"' in source
    assert "data.google_project.current.number" in source
    assert 'variable "project_number"' not in variables


def test_dev_environment_wires_c04_outputs_into_c05a() -> None:
    source = read(DEV_MAIN)

    assert 'source = "../../modules/ingress_policy"' in source
    assert 'source = "../../modules/run_services"' in source
    assert "depends_on = [module.managed_services]" in source
    assert "module.managed_services.artifact_registry.location" in source
    assert "module.managed_services.artifact_registry.repository_id" in source
    assert "module.managed_services.secret_containers.api_key_pepper" in source
    assert "module.managed_services.secret_containers.task_signing_hmac" in source
    assert "module.managed_services.firestore.name" in source


def test_c05a_docs_and_tests_cover_baseline_and_hardened_modes() -> None:
    readme = read(DEV_README)
    attack_path = read(ATTACK_PATH)
    ingress_test = read(INGRESS_POLICY_TEST)
    run_test = read(RUN_SERVICES_TEST)

    assert 'ingress_mode = "baseline"' in readme
    assert '"hardened"' in readme
    assert "default `run.app` URL is disabled" in attack_path
    assert "Worker ingress is never public" in readme
    assert "api_allow_unauthenticated" in ingress_test
    assert "public_invoker_with_disabled_api_url_fails_closed" in run_test
    assert "unbounded_retry_window_fails_closed" in run_test
    assert "invalid_digest_fails_closed" in run_test
    assert "invalid_worker_path_fails_closed" in run_test


def test_dev_environment_keeps_tasks_and_deployer_identities_distinct() -> None:
    variables = read(DEV_VARIABLES)

    assert (
        "var.tasks_service_account_email != var.deployer_service_account_email"
        in variables
    )
