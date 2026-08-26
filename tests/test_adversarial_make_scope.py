import subprocess
import sys
import tempfile
from pathlib import Path


def test_format_check_ignores_project_local_uv_cache() -> None:
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=".adversarial-uv-cache-", dir=repository
    ) as cache:
        cache_fixture = Path(cache) / "archive-v0" / "third-party"
        cache_fixture.mkdir(parents=True)
        (cache_fixture / "dependency.py").write_text("VALUE={1:2}\n", encoding="utf-8")
        result = subprocess.run(
            [str(Path(sys.executable).with_name("ruff")), "format", "--check", "."],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )

    assert result.returncode == 0, result.stdout + result.stderr
