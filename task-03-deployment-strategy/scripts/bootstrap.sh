#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP_TEMP_DIR=""

usage() {
  cat <<'EOF'
Usage:
  bootstrap.sh plan PROJECT_ID OWNER/REPOSITORY REVIEWER [REGION]
  bootstrap.sh apply PROJECT_ID OWNER/REPOSITORY REVIEWER [REGION]
  bootstrap.sh verify PROJECT_ID OWNER/REPOSITORY REVIEWER [REGION]
  bootstrap.sh deploy PROJECT_ID OWNER/REPOSITORY REVIEWER [REGION]

Optional environment:
  GCP_BILLING_ACCOUNT_ID
  GCP_BUDGET_NOTIFICATION_EMAILS   JSON array of email addresses
  TERRAFORM_BIN                    Defaults to terraform
EOF
}

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required"
}

cleanup() {
  [[ -z ${BOOTSTRAP_TEMP_DIR:-} ]] || rm -rf -- "$BOOTSTRAP_TEMP_DIR"
}

bucket_exists() {
  gcloud storage buckets describe "gs://$TF_VAR_state_bucket_name" \
    --project "$TF_VAR_project_id" >/dev/null 2>&1
}

initialize_bootstrap() {
  "$TERRAFORM_BIN" -chdir="$terraform_root" init \
    -input=false \
    -reconfigure \
    -backend-config="bucket=$TF_VAR_state_bucket_name" \
    -backend-config="prefix=assessment/bootstrap"
}

plan_bootstrap() {
  local plan_file=$1
  initialize_bootstrap
  "$TERRAFORM_BIN" -chdir="$terraform_root" plan \
    -input=false \
    -lock-timeout=60s \
    -out="$plan_file"
  "$TERRAFORM_BIN" -chdir="$terraform_root" show -no-color "$plan_file"
}

plan_state_bucket() {
  local plan_file=$1
  "$TERRAFORM_BIN" -chdir="$state_bucket_root" init -input=false
  "$TERRAFORM_BIN" -chdir="$state_bucket_root" plan \
    -input=false \
    -lock-timeout=60s \
    -out="$plan_file"
  "$TERRAFORM_BIN" -chdir="$state_bucket_root" show -no-color "$plan_file"
}

verify_bootstrap() {
  bucket_exists || fail "Terraform state bucket does not exist"
  initialize_bootstrap
  "$TERRAFORM_BIN" -chdir="$terraform_root" plan \
    -input=false \
    -lock-timeout=60s \
    -detailed-exitcode
  "$TERRAFORM_BIN" -chdir="$terraform_root" output -json github_delivery |
    jq -e --arg repository "$TF_VAR_github_repository" \
      '.repository == $repository and (.variables | length) >= 10' >/dev/null
  gcloud tasks queues describe "${TF_VAR_system_code}-${TF_VAR_environment}-run-dispatch" \
    --project "$TF_VAR_project_id" \
    --location "$TF_VAR_region" >/dev/null
  printf 'bootstrap verified for %s and %s\n' \
    "$TF_VAR_project_id" "$TF_VAR_github_repository"
}

main() {
  [[ $# -ge 4 && $# -le 5 ]] || { usage >&2; exit 2; }
  local command_name=$1 project_id=$2 repository=$3 reviewer=$4 region=${5:-europe-west3}
  [[ $command_name =~ ^(plan|apply|verify|deploy)$ ]] || { usage >&2; exit 2; }
  [[ $project_id =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || fail "invalid project id"
  [[ $repository =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "repository must use owner/name syntax"
  [[ $reviewer =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}$ ]] || fail "invalid reviewer login"
  [[ $region =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || fail "invalid region"

  require_tool gcloud
  require_tool gh
  require_tool git
  require_tool jq
  require_tool openssl
  TERRAFORM_BIN=${TERRAFORM_BIN:-terraform}
  [[ -x $TERRAFORM_BIN ]] || require_tool "$TERRAFORM_BIN"
  terraform_version=$("$TERRAFORM_BIN" version -json | jq -er '.terraform_version')
  [[ $terraform_version == 1.9.8 ]] || fail "Terraform 1.9.8 is required"

  repository_root=$(git rev-parse --show-toplevel)
  terraform_root="$repository_root/task-03-deployment-strategy/terraform/bootstrap"
  state_bucket_root="$repository_root/task-03-deployment-strategy/terraform/state_bucket"
  [[ -f $terraform_root/main.tf ]] || fail "run this command from the repository checkout"
  [[ -f $state_bucket_root/main.tf ]] || fail "state bucket Terraform root is missing"
  actual_project_number=$(
    gcloud projects describe "$project_id" --format='value(projectNumber)'
  )
  [[ $actual_project_number =~ ^[0-9]{6,20}$ ]] || fail "could not resolve the project number"
  gh repo view "$repository" --json nameWithOwner --jq '.nameWithOwner' >/dev/null

  if [[ -z ${GITHUB_TOKEN:-} ]]; then
    GITHUB_TOKEN=$(gh auth token)
    export GITHUB_TOKEN
  fi
  unset GITHUB_OWNER GITHUB_ORGANIZATION

  export TF_IN_AUTOMATION=true
  export TF_INPUT=false
  export TF_VAR_project_id=$project_id
  export TF_VAR_region=$region
  export TF_VAR_environment=dev
  export TF_VAR_system_code=sai
  export TF_VAR_state_bucket_name="${project_id}-sai-tf-state"
  export TF_VAR_enable_github_wif=true
  export TF_VAR_github_repository=$repository
  export TF_VAR_github_branch=master
  export TF_VAR_github_environment=gcp-dev
  export TF_VAR_github_reviewer=$reviewer
  export TF_VAR_seed_secret_versions=true
  export TF_VAR_billing_account_id=${GCP_BILLING_ACCOUNT_ID:-}
  export TF_VAR_budget_notification_emails=${GCP_BUDGET_NOTIFICATION_EMAILS:-[]}

  if [[ $command_name == verify ]]; then
    verify_bootstrap
    exit 0
  fi

  BOOTSTRAP_TEMP_DIR=$(mktemp -d -t sai-bootstrap.XXXXXX)
  trap cleanup EXIT

  if ! bucket_exists; then
    state_plan="$BOOTSTRAP_TEMP_DIR/state-bucket.tfplan"
    plan_state_bucket "$state_plan"
    if [[ $command_name == plan ]]; then
      printf '%s\n' \
        "bootstrap plan is available after Terraform creates the state bucket"
      exit 0
    fi
    "$TERRAFORM_BIN" -chdir="$state_bucket_root" apply \
      -input=false \
      -lock-timeout=60s \
      "$state_plan"
    bucket_exists || fail "Terraform did not create the state bucket"
  fi

  plan_file="$BOOTSTRAP_TEMP_DIR/bootstrap.tfplan"
  plan_bootstrap "$plan_file"
  [[ $command_name != plan ]] || exit 0

  "$TERRAFORM_BIN" -chdir="$terraform_root" apply \
    -input=false \
    -lock-timeout=60s \
    "$plan_file"
  verify_bootstrap

  if [[ $command_name == deploy ]]; then
    local_revision=$(git -C "$repository_root" rev-parse HEAD)
    remote_revision=$(gh api "repos/$repository/commits/master" --jq '.sha')
    [[ $local_revision == "$remote_revision" ]] ||
      fail "push the current master revision before dispatching deployment"
    gh workflow run deploy.yml --repo "$repository" --ref master
    printf 'deployment workflow dispatched for %s at %s\n' \
      "$repository" "$local_revision"
  fi
}

main "$@"
