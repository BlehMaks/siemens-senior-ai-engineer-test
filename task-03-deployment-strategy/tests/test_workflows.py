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

        if name == "infra-plan.yml":
            assert "github.ref == 'refs/heads/master'" in source
        else:
            assert "Bind dispatch to the verified revision" in source
            assert '"$GITHUB_REF" != refs/heads/master' in source
            assert '"$GITHUB_SHA" != "$EXPECTED_SHA"' in source
            assert "needs: revision" in source
            assert "if: needs.revision.result == 'success'" in source
        assert "environment: gcp-dev" in source
        assert "id-token: write" in source
        assert "google-github-actions/auth@" in source
        assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in source
        assert "delegates:" not in source
        assert "GCP_DEPLOYER_SERVICE_ACCOUNT" in source


def test_deploy_promotes_the_tested_artifact_by_digest() -> None:
    source = read(WORKFLOW_ROOT / "deploy.yml")

    assert "run-name: sai-deploy-${{ inputs.dispatch_id }}" in source
    assert "dispatch_id:" in source
    assert "expected_sha:" in source
    assert "needs: verify" in source
    assert "docker save --output release-image.tar" in source
    assert "docker load --input release-image.tar" in source
    assert "docker buildx imagetools inspect" in source
    assert "oauth2accesstoken" in source
    assert '"$registry_digest" != "$push_digest"' in source
    assert "^sha256:[a-f0-9]{64}$" in source
    assert "TF_VAR_image_digest: ${{ steps.image.outputs.digest }}" in source
    assert "apply only the reviewed plan" in source.lower()
    assert "dev.tfplan" in source
    assert "reviewed-plan-${{ github.run_id }}" in source
    assert "needs.plan.outputs.image_digest" in source
    assert "AGENT_API_KEY_PEPPER" not in source
    assert "AGENT_API_TASK_SIGNING_HMAC" not in source
    assert "gcloud" not in source
    assert 'terraform -chdir="$environment_root" refresh' not in source
    assert "TF_VAR_model_plane_profile: assessment" in source


def test_remote_state_and_secret_container_inputs_fail_closed() -> None:
    deploy = read(WORKFLOW_ROOT / "deploy.yml")
    plan = read(WORKFLOW_ROOT / "infra-plan.yml")

    for source in (deploy, plan):
        assert "-backend-config=bucket=$TF_STATE_BUCKET" in source
        assert "-backend-config=prefix=assessment/dev" in source
        assert "TF_VAR_secret_ids: ${{ vars.GCP_SECRET_IDS }}" in source
        assert (
            "TF_VAR_budget_amount_units: ${{ vars.GCP_BUDGET_AMOUNT_UNITS }}" in source
        )
        assert "TF_VAR_budget_amount_units must be a positive whole number" in source
        assert 'and (keys | sort) == ["api_key_pepper", "task_signing_hmac"]' in source
        assert "expected exactly two non-empty secret container IDs" in source

    assert ".secret_containers." not in deploy
    assert "gcloud secrets versions list" not in deploy
    assert "terraform init -backend=false" not in deploy


def test_deploy_aligns_firestore_index_state_before_planning() -> None:
    source = read(WORKFLOW_ROOT / "deploy.yml")
    migration = source.index("- name: Align legacy Firestore index state")
    foundation = source.index(
        "- name: Create the managed foundation on first deployment"
    )

    assert migration < foundation
    assert "scripts/migrate_firestore_index_state.sh" in source[migration:foundation]
    assert (
        '"$GITHUB_WORKSPACE/task-03-deployment-strategy/terraform/environments/dev"'
        in source[migration:foundation]
    )
    assert '"$TF_VAR_project_id"' in source[migration:foundation]
    assert '"sai-dev"' in source[migration:foundation]


def test_ci_runs_the_cross_module_environment_contract() -> None:
    source = read(WORKFLOW_ROOT / "ci.yml")
    contract_step = source.split(
        "- name: Run Terraform resource contracts", maxsplit=1
    )[1]

    assert "environments/dev" in contract_step
