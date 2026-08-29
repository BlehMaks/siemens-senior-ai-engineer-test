from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_submission_check.sh"


def test_local_submission_check_covers_all_six_tasks_and_agent_smoke() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert SCRIPT.stat().st_mode & 0o111
    assert "uv sync --locked --all-packages --all-groups" in source
    assert "uv run --frozen pytest -q" in source
    assert "audit_submission.py" in source
    assert "local_acceptance.sh" in source
    assert "SIEMENS_TASK4_INPUT_DIR" in source
    assert "SIEMENS_FUSE_CSV" in source
    assert "Tasks 1 through 6" in source
