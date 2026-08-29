from __future__ import annotations

import os
import subprocess
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = TASK_ROOT / "scripts" / "bootstrap.sh"
SEED_SECRET = TASK_ROOT / "scripts" / "seed_secret_version.sh"


def _executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def test_bootstrap_routes_all_cloud_mutations_through_terraform() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")

    assert BOOTSTRAP.stat().st_mode & 0o111
    assert '"$TERRAFORM_BIN" -chdir="$state_bucket_root" apply' in source
    assert '"$TERRAFORM_BIN" -chdir="$terraform_root" apply' in source
    assert '-backend-config="bucket=$TF_VAR_state_bucket_name"' in source
    assert "gh workflow run deploy.yml" in source
    for direct_mutation in (
        "gcloud run deploy",
        "gcloud run services update",
        "gcloud iam service-accounts create",
        "gcloud secrets create",
        "gcloud tasks queues create",
    ):
        assert direct_mutation not in source


def test_secret_seed_is_idempotent_and_keeps_payload_off_command_line(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "gcloud.log"
    _executable(
        fake_bin / "gcloud",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_GCLOUD_LOG"
if [[ $1 == secrets && $2 == versions && $3 == list ]]; then
  printf '%s' "${FAKE_EXISTING_VERSION:-}"
elif [[ $1 == secrets && $2 == versions && $3 == add ]]; then
  payload=$(cat)
  [[ $payload =~ ^[A-Za-z0-9_-]{64}$ ]]
else
  exit 9
fi
""",
    )
    _executable(
        fake_bin / "openssl",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' 'QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB'
""",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_GCLOUD_LOG": str(log),
        "GCP_PROJECT_ID": "contract-assignment-dev",
        "GCP_SECRET_ID": "sai-dev-api-key-pepper",
    }

    subprocess.run([str(SEED_SECRET)], env=env, check=True, capture_output=True)
    first_log = log.read_text(encoding="utf-8")
    assert "secrets versions add sai-dev-api-key-pepper" in first_log
    assert "QUFB" not in first_log

    env["FAKE_EXISTING_VERSION"] = "projects/example/secrets/example/versions/1"
    subprocess.run([str(SEED_SECRET)], env=env, check=True, capture_output=True)
    assert log.read_text(encoding="utf-8").count("secrets versions add") == 1
