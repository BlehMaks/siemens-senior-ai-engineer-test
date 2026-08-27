from __future__ import annotations

import re
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
CONTAINER = TASK_ROOT / "src" / "deployment_strategy" / "container.py"
RUN_SERVICES = TASK_ROOT / "terraform" / "modules" / "run_services" / "main.tf"
RUN_OUTPUTS = TASK_ROOT / "terraform" / "modules" / "run_services" / "outputs.tf"
BOOTSTRAP = TASK_ROOT / "terraform" / "bootstrap" / "main.tf"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_terraform_and_container_share_the_exact_cloud_configuration_contract() -> None:
    container = read(CONTAINER)
    terraform = read(RUN_SERVICES)
    names = {
        "AGENT_API_SERVICE_ROLE",
        "AGENT_API_GCP_PROJECT_ID",
        "AGENT_API_FIRESTORE_DATABASE",
        "AGENT_API_CLOUD_TASKS_QUEUE",
        "AGENT_API_TASK_TARGET_URL",
        "AGENT_API_QUEUE_DELIVERY_PATH",
        "AGENT_API_TASK_SIGNING_HMAC",
    }

    for name in names:
        assert name in container
        assert name in terraform
    assert re.search(r'AGENT_API_SERVICE_ROLE\s*=\s*"api"', terraform)
    assert re.search(r'AGENT_API_SERVICE_ROLE\s*=\s*"worker"', terraform)
    assert terraform.count('name = "AGENT_API_TASK_SIGNING_HMAC"') == 2
    assert (
        '"${google_cloud_run_v2_service.worker.uri}${var.worker_dispatch_path}"'
        in terraform
    )


def test_runtime_identities_have_only_the_queue_operations_the_code_uses() -> None:
    terraform = read(BOOTSTRAP)
    outputs = read(RUN_OUTPUTS)

    assert 'role   = "roles/cloudtasks.enqueuer"' in terraform
    assert 'role   = "roles/cloudtasks.viewer"' in terraform
    assert 'role   = "roles/cloudtasks.taskDeleter"' in terraform
    assert 'module.identity["api"].email' in terraform
    assert 'module.identity["worker"].email' in terraform
    assert 'task_viewer_role               = "roles/cloudtasks.viewer"' in outputs
    assert 'task_deleter_role              = "roles/cloudtasks.taskDeleter"' in outputs
