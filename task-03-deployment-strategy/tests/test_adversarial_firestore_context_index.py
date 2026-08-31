import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_REPOSITORY = (
    ROOT.parent / "task-02-agent-api" / "src" / "agent_api" / "storage" / "cloud.py"
)
MANAGED_SERVICES = ROOT / "terraform" / "modules" / "managed_services" / "main.tf"


def test_adversarial_recent_context_query_has_matching_descending_index() -> None:
    repository = RUN_REPOSITORY.read_text()
    assert 'order_by=("-created_at", "-run_id")' in repository

    terraform = MANAGED_SERVICES.read_text()
    runs_index = terraform.split(
        'resource "google_firestore_index" "runs"', maxsplit=1
    )[1].split('\nresource "', maxsplit=1)[0]

    assert 'field_path = "created_at"\n    order      = "DESCENDING"' in runs_index
    assert 'field_path = "run_id"\n    order      = "DESCENDING"' in runs_index


def test_completed_context_query_has_matching_filtered_index() -> None:
    repository = RUN_REPOSITORY.read_text()
    completed_query = repository.split("async def list_session_completed(", maxsplit=1)[
        1
    ].split("\n    async def ", maxsplit=1)[0]
    assert '"state": RunState.COMPLETED.value' in completed_query
    assert 'order_by=("-created_at", "-run_id")' in completed_query

    terraform = MANAGED_SERVICES.read_text()
    completed_index = terraform.split(
        'resource "google_firestore_index" "runs_completed"', maxsplit=1
    )[1].split('\nresource "', maxsplit=1)[0]
    fields = re.findall(
        r'field_path = "([^"]+)"\n\s+order\s+= "([A-Z]+)"', completed_index
    )

    assert fields == [
        ("tenant_id", "ASCENDING"),
        ("session_id", "ASCENDING"),
        ("state", "ASCENDING"),
        ("created_at", "DESCENDING"),
        ("run_id", "DESCENDING"),
    ]
    assert "prevent_destroy = true" in completed_index
