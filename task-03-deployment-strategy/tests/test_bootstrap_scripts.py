from __future__ import annotations

import os
import subprocess
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = TASK_ROOT / "scripts" / "bootstrap.sh"
SECRET_VERSION_SCRIPT = TASK_ROOT / "scripts" / "seed_secret_version.sh"


def _executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def test_bootstrap_routes_all_cloud_mutations_through_terraform() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")

    assert BOOTSTRAP.stat().st_mode & 0o111
    assert '"$TERRAFORM_BIN" -chdir="$state_bucket_root" apply' in source
    assert '"$TERRAFORM_BIN" -chdir="$terraform_root" apply' in source
    assert '-backend-config="bucket=$TF_VAR_bootstrap_state_bucket_name"' in source
    assert (
        'TF_VAR_application_state_bucket_name="${project_id}-sai-app-tf-state"'
        in source
    )
    assert "GCP_IMPORT_STATE_BUCKETS" in source
    assert "TF_VAR_existing_state_buckets" in source
    assert "output -raw project_number" in source
    assert "resolve_budget_coordinates" in source
    assert "GCP_BILLING_ACCOUNT_ID must be the linked billing account ID" in source
    assert "GCP_BUDGET_AMOUNT_UNITS must be a positive whole number" in source
    assert "GCP_BUDGET_NOTIFICATION_EMAILS" in source
    assert "verify_application_cost_controls" in source
    assert ".budget.amount_units == $amount" in source
    assert ".api_service.max_instances == 1" in source
    assert ".worker_service.max_instances == 1" in source
    assert '-f "dispatch_id=$dispatch_id"' in source
    assert '-f "expected_sha=$revision"' in source
    assert 'run_title="sai-deploy-$dispatch_id"' in source
    assert 'gh run watch "$run_id"' in source
    assert "TF_VAR_enable_runtime_policy=true" in source
    assert "output -raw secret_version_count" in source
    assert "gh workflow run deploy.yml" in source
    assert ".branch_protection.admin_enforcement == true" in source
    assert ".firestore_database_name == $database" in source
    assert "gcloud" not in source


def test_runtime_policy_detection_survives_pipefail(tmp_path: Path) -> None:
    fake_terraform = tmp_path / "terraform"
    _executable(
        fake_terraform,
        """#!/usr/bin/env bash
set -euo pipefail
[[ ${2:-} == state && ${3:-} == list ]]
printf '%s\n' 'google_cloud_run_v2_service_iam_binding.worker_invoker[0]'
for index in {1..20000}; do
  printf 'unrelated_resource.%s\n' "$index"
done
""",
    )
    script_prefix = BOOTSTRAP.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    probe = tmp_path / "probe.sh"
    _executable(
        probe,
        script_prefix
        + """
TERRAFORM_BIN=$1
terraform_root=unused
select_runtime_policy_mode
printf '%s\n' "$TF_VAR_enable_runtime_policy"
""",
    )

    completed = subprocess.run(
        [str(probe), str(fake_terraform)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == "true\n"


def test_runtime_policy_detection_stops_when_state_read_fails(
    tmp_path: Path,
) -> None:
    fake_terraform = tmp_path / "terraform"
    _executable(
        fake_terraform,
        """#!/usr/bin/env bash
set -euo pipefail
[[ ${2:-} == state && ${3:-} == list ]]
printf '%s\n' 'backend unavailable' >&2
exit 23
""",
    )
    script_prefix = BOOTSTRAP.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    probe = tmp_path / "probe.sh"
    _executable(
        probe,
        script_prefix
        + """
TERRAFORM_BIN=$1
terraform_root=unused
select_runtime_policy_mode
printf '%s\n' reached-plan
""",
    )

    completed = subprocess.run(
        [str(probe), str(fake_terraform)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "reached-plan" not in completed.stdout
    assert "Terraform could not read bootstrap state" in completed.stderr


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

    subprocess.run(
        [str(SECRET_VERSION_SCRIPT)], env=env, check=True, capture_output=True
    )
    first_log = log.read_text(encoding="utf-8")
    assert "secrets versions add sai-dev-api-key-pepper" in first_log
    assert "QUFB" not in first_log

    env["FAKE_EXISTING_VERSION"] = "projects/example/secrets/example/versions/1"
    subprocess.run(
        [str(SECRET_VERSION_SCRIPT)], env=env, check=True, capture_output=True
    )
    assert log.read_text(encoding="utf-8").count("secrets versions add") == 1


def test_budget_recipient_rejects_mixed_case_service_account_domain(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "gcloud",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ $1 == billing && $2 == accounts && $3 == describe ]]; then
  printf '%s\n' EUR
else
  exit 91
fi
""",
    )
    script_prefix = BOOTSTRAP.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    probe = tmp_path / "probe.sh"
    _executable(
        probe,
        script_prefix
        + """
resolve_budget_coordinates contract-assignment-dev
""",
    )

    completed = subprocess.run(
        [str(probe)],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "GCP_BILLING_ACCOUNT_ID": "ABC123-DEF456-789ABC",
            "GCP_BUDGET_NOTIFICATION_EMAILS": (
                '["automation@contract-assignment-dev.IAM.GSERVICEACCOUNT.COM"]'
            ),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
