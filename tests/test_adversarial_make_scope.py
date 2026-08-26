import shutil
import subprocess
from pathlib import Path


def test_format_check_ignores_project_local_uv_cache() -> None:
    repository = Path(__file__).resolve().parents[1]
    cache_root = repository / ".adversarial-uv-cache"
    cache_fixture = cache_root / "archive-v0" / "third-party"
    cache_fixture.mkdir(parents=True, exist_ok=True)
    (cache_fixture / "dependency.py").write_text("VALUE={1:2}\n", encoding="utf-8")

    try:
        result = subprocess.run(
            [shutil.which("ruff") or "ruff", "format", "--check", "."],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(cache_root)

    assert result.returncode == 0, result.stdout + result.stderr
