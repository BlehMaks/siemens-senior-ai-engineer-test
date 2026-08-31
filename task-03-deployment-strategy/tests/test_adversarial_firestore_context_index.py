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
    )[1].split('resource "google_firestore_index" "run_events"', maxsplit=1)[0]

    assert 'field_path = "created_at"\n    order      = "DESCENDING"' in runs_index
    assert 'field_path = "run_id"\n    order      = "DESCENDING"' in runs_index
