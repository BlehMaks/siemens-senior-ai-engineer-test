from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
LOCAL_ACCEPTANCE = TASK_ROOT / "scripts" / "local_acceptance.sh"


def test_local_acceptance_installs_every_workspace_package_and_hides_keys() -> None:
    source = LOCAL_ACCEPTANCE.read_text(encoding="utf-8")

    assert LOCAL_ACCEPTANCE.stat().st_mode & 0o111
    assert 'UV_PROJECT_ENVIRONMENT="$LOCAL_ACCEPTANCE_TEMP_DIR/venv"' in source
    assert "uv sync --locked --all-packages --dev" in source
    assert source.count("--scope runs:write") == 2
    assert "api_smoke.sh" in source
    assert "printf '%s' \"$api_key" not in source
