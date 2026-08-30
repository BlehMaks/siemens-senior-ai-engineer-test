from __future__ import annotations

from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
TERRAFORM_ROOT = TASK_ROOT / "terraform"
RUN_SERVICES_MAIN = TERRAFORM_ROOT / "modules" / "run_services" / "main.tf"
RUN_SERVICES_OUTPUTS = TERRAFORM_ROOT / "modules" / "run_services" / "outputs.tf"
RUN_SERVICES_VARIABLES = TERRAFORM_ROOT / "modules" / "run_services" / "variables.tf"
BOOTSTRAP_MAIN = TERRAFORM_ROOT / "bootstrap" / "main.tf"
BOOTSTRAP_VARIABLES = TERRAFORM_ROOT / "bootstrap" / "variables.tf"
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
BOOTSTRAP_TEST = TERRAFORM_ROOT / "bootstrap" / "tests" / "c03_wif_contract.tftest.hcl"
DEV_MAIN = TERRAFORM_ROOT / "environments" / "dev" / "main.tf"
DEV_README = TERRAFORM_ROOT / "environments" / "dev" / "README.md"
ATTACK_PATH = TERRAFORM_ROOT / "environments" / "dev" / "c05a-attack-path.md"
MODEL_PLANE_MAIN = TERRAFORM_ROOT / "modules" / "model_plane" / "main.tf"
MODEL_PLANE_VARIABLES = TERRAFORM_ROOT / "modules" / "model_plane" / "variables.tf"
MODEL_PLANE_SCRIPT = TASK_ROOT / "scripts" / "model_plane.sh"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_run_services_keeps_worker_private_and_oidc_bound() -> None:
    source = read(RUN_SERVICES_MAIN)
    outputs = read(RUN_SERVICES_OUTPUTS)
    bootstrap = read(BOOTSTRAP_MAIN)

    assert source.count("invoker_iam_disabled = false") == 2
    assert "ingress              = var.worker_ingress" in source
    assert "google_cloud_run_v2_service_iam_" not in source
    assert (
        'resource "google_cloud_run_v2_service_iam_binding" "worker_invoker"'
        in bootstrap
    )
    assert (
        'members  = ["serviceAccount:${module.identity["tasks"].email}"]' in bootstrap
    )
    assert (
        'resource "google_cloud_run_v2_service_iam_member" "api_public_invoker"'
        in bootstrap
    )
    assert "google_cloud_tasks_queue_iam_" not in source
    assert "google_service_account_iam_" not in source
    assert 'worker_invoker_role            = "roles/run.invoker"' in outputs
    assert 'api_queue_enqueuer_role        = "roles/cloudtasks.enqueuer"' in outputs
    assert (
        'tasks_service_agent_token_role = "roles/iam.serviceAccountTokenCreator"'
        in outputs
    )
    assert 'resource "google_cloud_tasks_queue"' not in source
    assert 'service_account_email = module.identity["tasks"].email' in bootstrap
    assert "audience              = local.worker_service_url" in bootstrap
    assert bootstrap.count('member   = "allUsers"') == 1
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
    bootstrap_variables = read(BOOTSTRAP_VARIABLES)
    bootstrap = read(BOOTSTRAP_MAIN)

    assert 'regex("^sha256:[a-f0-9]{64}$"' in variables
    assert "min_instance_count = 0" in source
    assert "max_instance_count = var.api_max_instances" in source
    assert "max_instance_count = var.worker_max_instances" in source
    assert (
        "max_dispatches_per_second = var.queue_max_dispatches_per_second" in bootstrap
    )
    assert (
        "max_concurrent_dispatches = var.queue_max_concurrent_dispatches" in bootstrap
    )
    assert "max_attempts       = var.queue_max_attempts" in bootstrap
    assert "prevent_destroy = true" in bootstrap
    assert "var.queue_max_retry_seconds >= 1" in bootstrap_variables


def test_bootstrap_owns_the_cloud_tasks_queue_before_application_deploy() -> None:
    source = read(RUN_SERVICES_MAIN)
    bootstrap = read(BOOTSTRAP_MAIN)

    assert 'resource "google_cloud_tasks_queue" "dispatch"' not in source
    assert 'resource "google_cloud_tasks_queue" "dispatch"' in bootstrap
    assert "enable_runtime_policy" in bootstrap
    assert "local.worker_service_url" in bootstrap
    assert "cloudtasks.queues.update" not in read(
        TERRAFORM_ROOT / "bootstrap" / "locals.tf"
    )


def test_dev_environment_wires_c04_outputs_into_c05a() -> None:
    source = read(DEV_MAIN)

    assert 'source = "../../modules/ingress_policy"' in source
    assert 'source = "../../modules/run_services"' in source
    assert "depends_on = [module.managed_services]" in source
    assert "module.managed_services.artifact_registry.location" in source
    assert "module.managed_services.artifact_registry.repository_id" in source
    assert "var.secret_ids.api_key_pepper" in source
    assert "var.secret_ids.task_signing_hmac" in source
    assert "module.managed_services.firestore.name" in source


def test_c05a_docs_and_tests_cover_baseline_and_hardened_modes() -> None:
    readme = read(DEV_README)
    attack_path = read(ATTACK_PATH)
    ingress_test = read(INGRESS_POLICY_TEST)
    run_test = read(RUN_SERVICES_TEST)
    bootstrap_test = read(BOOTSTRAP_TEST)

    assert 'ingress_mode = "baseline"' in readme
    assert '"hardened"' in readme
    assert "default `run.app` URL is disabled" in attack_path
    assert "Worker ingress is never public" in readme
    assert "api_allow_unauthenticated" in ingress_test
    assert "public_invoker_with_disabled_api_url_fails_closed" in run_test
    assert "unbounded_retry_window_fails_closed" in bootstrap_test
    assert "invalid_digest_fails_closed" in run_test
    assert "invalid_worker_path_fails_closed" in run_test


def test_dev_environment_keeps_tasks_and_deployer_identities_distinct() -> None:
    variables = read(DEV_VARIABLES)

    assert (
        "var.tasks_service_account_email != var.deployer_service_account_email"
        in variables
    )
    assert 'var.model_plane_profile == "assessment"' in variables


def test_production_model_plane_is_explicit_private_and_bounded() -> None:
    source = read(MODEL_PLANE_MAIN)
    variables = read(MODEL_PLANE_VARIABLES)

    assert 'default     = "assessment"' in variables
    assert 'contains(["assessment", "cloud_run_gpu"]' in variables
    assert '"nvidia.com/gpu" = "1"' in source
    assert 'accelerator = "nvidia-l4"' in source
    assert "min_instance_count = var.model_min_instances" in source
    assert "max_instance_count = var.model_max_instances" in source
    assert 'ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"' in source
    assert 'role     = "roles/run.invoker"' in source
    assert 'member   = "allUsers"' not in source
    assert "deletion_protection  = true" in source


def test_model_plane_script_requires_cost_ack_and_never_calls_gcloud() -> None:
    source = read(MODEL_PLANE_SCRIPT)

    assert "MODEL_PLANE_COST_ACKNOWLEDGEMENT=I_ACCEPT_GPU_COSTS" in source
    assert '"$TERRAFORM_BIN" -chdir="$terraform_root" apply' in source
    assert "gcloud " not in source
