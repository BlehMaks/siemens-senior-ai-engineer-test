from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

TASK_ROOT = Path(__file__).resolve().parents[1]
CLOUD_API_SMOKE = TASK_ROOT / "scripts" / "cloud_api_smoke.sh"


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path, *, smoke_exit: int = 0) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "repository"
    scripts = root / "task-03-deployment-strategy" / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "cloud_api_smoke.sh"
    shutil.copy2(CLOUD_API_SMOKE, wrapper)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    _write_executable(
        fake_bin / "gcloud",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'gcloud %s\n' "$*" >> "$FAKE_COMMAND_LOG"
case "$1 $2" in
  "projects describe") printf '1027058459333\n' ;;
  "run services") printf 'https://sai-dev-api-example.run.app\n' ;;
  "secrets versions") printf 'test-pepper-value\n' ;;
  *) printf 'unexpected gcloud command: %s\n' "$*" >&2; exit 9 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'uv %s\n' "$*" >> "$FAKE_COMMAND_LOG"
if [[ " $* " != *" agent-api-key-admin "* ]]; then
  exit 0
fi
if [[ " $* " == *" create "* && " $* " == *" smoke-review-001-a "* ]]; then
  [[ ${AGENT_API_KEY_PEPPER:-} == test-pepper-value ]]
  printf 'temporary-key-a\n'
elif [[ " $* " == *" create "* && " $* " == *" smoke-review-001-b "* ]]; then
  [[ ${AGENT_API_KEY_PEPPER:-} == test-pepper-value ]]
  printf 'temporary-key-b\n'
elif [[ " $* " == *" revoke "* ]]; then
  [[ ${AGENT_API_KEY_PEPPER:-} == test-pepper-value ]]
  [[ ${AGENT_API_AUTHORIZATION:-} == "Bearer temporary-key-a" || ${AGENT_API_AUTHORIZATION:-} == "Bearer temporary-key-b" ]]
else
  printf 'unexpected uv command: %s\n' "$*" >&2
  exit 8
fi
""",
    )
    _write_executable(fake_bin / "jq", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(fake_bin / "curl", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        scripts / "api_smoke.sh",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'api-smoke %s\n' "$*" >> "$FAKE_COMMAND_LOG"
[[ $1 == https://sai-dev-api-example.run.app ]]
[[ $2 == review-001 ]]
[[ $SMOKE_API_KEY_A == temporary-key-a ]]
[[ $SMOKE_API_KEY_B == temporary-key-b ]]
exit {smoke_exit}
""",
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "UV_BIN": str(fake_bin / "uv"),
        "FAKE_COMMAND_LOG": str(log),
    }
    return wrapper, environment


def _run(wrapper: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(wrapper),
            "liquidity-planning-platform",
            "europe-west3",
            "dev",
            "1027058459333",
            "review-001",
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def test_cloud_smoke_creates_runs_and_revokes_two_tenant_keys(tmp_path: Path) -> None:
    wrapper, environment = _fixture(tmp_path)

    result = _run(wrapper, environment)

    assert result.returncode == 0, result.stderr
    assert "cloud API smoke passed and temporary keys were revoked" in result.stdout
    assert "temporary-key" not in result.stdout + result.stderr
    assert "test-pepper-value" not in result.stdout + result.stderr
    log = Path(environment["FAKE_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert log.count(" agent-api-key-admin ") == 4
    assert log.count(" revoke") == 2
    assert log.count("--firestore-database sai-dev") == 4
    assert "api-smoke https://sai-dev-api-example.run.app review-001" in log


def test_cloud_smoke_revokes_both_keys_when_http_smoke_fails(tmp_path: Path) -> None:
    wrapper, environment = _fixture(tmp_path, smoke_exit=7)

    result = _run(wrapper, environment)

    assert result.returncode == 7
    log = Path(environment["FAKE_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert log.count(" revoke") == 2
    assert "cloud API smoke passed" not in result.stdout


@pytest.mark.parametrize(
    ("argument_index", "value", "message"),
    [
        (2, "prod", "only the reviewed dev environment is supported"),
        (3, "1027058459334", "project number does not match"),
    ],
)
def test_cloud_smoke_rejects_a_wrong_boundary(
    tmp_path: Path, argument_index: int, value: str, message: str
) -> None:
    wrapper, environment = _fixture(tmp_path)
    arguments = [
        "liquidity-planning-platform",
        "europe-west3",
        "dev",
        "1027058459333",
        "review-001",
    ]
    arguments[argument_index] = value

    result = subprocess.run(
        [str(wrapper), *arguments],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode != 0
    assert message in result.stderr
