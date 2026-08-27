from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPOSITORY_ROOT / ".github" / "workflows"
WORKFLOWS = tuple(sorted(WORKFLOW_ROOT.glob("*.yml")))
FULL_ACTION_PIN = re.compile(
    r"^\s*uses:\s*[^\s@]+@[0-9a-f]{40}(?:\s*#.*)?$", re.MULTILINE
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_expected_workflows_are_the_only_delivery_entrypoints() -> None:
    assert {path.name for path in WORKFLOWS} == {
        "ci.yml",
        "deploy.yml",
        "infra-plan.yml",
    }


def test_actions_are_immutable_and_workflows_default_to_no_permissions() -> None:
    for path in WORKFLOWS:
        source = read(path)
        action_steps = [line for line in source.splitlines() if "uses:" in line]

        assert action_steps
        assert len(FULL_ACTION_PIN.findall(source)) == len(action_steps)
        assert "\npermissions: {}\n" in source
        assert "pull_request_target" not in source
        assert "secrets." not in source


def test_untrusted_ci_has_no_cloud_identity_or_environment_access() -> None:
    source = read(WORKFLOW_ROOT / "ci.yml")

    assert "pull_request:" in source
    assert "id-token: write" not in source
    assert "environment:" not in source
    assert "google-github-actions/auth" not in source


def test_privileged_jobs_are_master_and_environment_gated() -> None:
    for name in ("infra-plan.yml", "deploy.yml"):
        source = read(WORKFLOW_ROOT / name)

        assert "github.ref == 'refs/heads/master'" in source
        assert "environment: gcp-dev" in source
        assert "id-token: write" in source
        assert "google-github-actions/auth@" in source
        assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in source
        assert "delegates: ${{ vars.GCP_CI_SERVICE_ACCOUNT }}" in source
        assert "GCP_DEPLOYER_SERVICE_ACCOUNT" in source


def test_deploy_promotes_the_tested_artifact_by_digest() -> None:
    source = read(WORKFLOW_ROOT / "deploy.yml")

    assert "needs: verify" in source
    assert "docker save --output release-image.tar" in source
    assert "docker load --input release-image.tar" in source
    assert "gcloud artifacts docker images describe" in source
    assert '"$registry_digest" != "$push_digest"' in source
    assert "^sha256:[a-f0-9]{64}$" in source
    assert "TF_VAR_image_digest: ${{ steps.image.outputs.digest }}" in source
    assert "apply only the reviewed plan" in source.lower()
    assert "dev.tfplan" in source
    assert "reviewed-plan-${{ github.run_id }}" in source
    assert "needs.plan.outputs.image_digest" in source
    assert "AGENT_API_KEY_PEPPER" not in source
    assert "AGENT_API_TASK_SIGNING_HMAC" not in source


def test_remote_state_and_secret_container_guards_fail_closed() -> None:
    deploy = read(WORKFLOW_ROOT / "deploy.yml")
    plan = read(WORKFLOW_ROOT / "infra-plan.yml")

    for source in (deploy, plan):
        assert "-backend-config=bucket=$TF_STATE_BUCKET" in source
        assert "-backend-config=prefix=assessment/dev" in source

    assert "gcloud secrets versions list" in deploy
    assert "has no enabled version" in deploy
    assert "terraform init -backend=false" not in deploy


def test_ci_runs_the_cross_module_environment_contract() -> None:
    source = read(WORKFLOW_ROOT / "ci.yml")
    contract_step = source.split(
        "- name: Run Terraform resource contracts", maxsplit=1
    )[1]

    assert "environments/dev" in contract_step
