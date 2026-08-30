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

Required environment:
  GCP_BILLING_ACCOUNT_ID           Linked billing account ID
  GCP_BUDGET_NOTIFICATION_EMAILS   Monitored addresses as a JSON array

Optional environment:
  GCP_IMPORT_STATE_BUCKETS         Set to true only when adopting both named buckets
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

resolve_budget_coordinates() {
  [[ ${GCP_BILLING_ACCOUNT_ID:-} =~ ^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$ ]] ||
    fail "GCP_BILLING_ACCOUNT_ID must be the linked billing account ID"
  resolved_budget_notification_emails=$(jq -cer '
    if type == "array"
      and length > 0
      and all(.[];
        type == "string"
        and test("^[^@[:space:]]+@[^@[:space:].]+(\\.[^@[:space:].]+)+$")
        and ((ascii_downcase | endswith(".gserviceaccount.com")) | not)
      )
    then sort | unique
    else error("expected at least one budget notification email")
    end
  ' <<<"${GCP_BUDGET_NOTIFICATION_EMAILS:-}") ||
    fail "GCP_BUDGET_NOTIFICATION_EMAILS is invalid"
}

cleanup() {
  [[ -z ${BOOTSTRAP_TEMP_DIR:-} ]] || rm -rf -- "$BOOTSTRAP_TEMP_DIR"
}

all_state_buckets_exist() {
  [[ $("$TERRAFORM_BIN" -chdir="$state_bucket_root" state list 2>/dev/null |
    grep -c '^google_storage_bucket\.terraform_state') -eq 2 ]]
}

initialize_state_buckets() {
  local state_directory="$repository_root/.local/terraform"
  mkdir -p "$state_directory"
  "$TERRAFORM_BIN" -chdir="$state_bucket_root" init \
    -input=false \
    -reconfigure \
    -backend-config="path=$state_directory/${TF_VAR_project_id}-state-buckets.tfstate"
}

plan_state_buckets() {
  local plan_file=$1
  "$TERRAFORM_BIN" -chdir="$state_bucket_root" plan \
    -input=false \
    -lock-timeout=60s \
    -out="$plan_file"
  "$TERRAFORM_BIN" -chdir="$state_bucket_root" show -no-color "$plan_file"
}

verify_state_buckets() {
  local names
  all_state_buckets_exist || fail "Terraform does not manage both state buckets"
  names=$("$TERRAFORM_BIN" -chdir="$state_bucket_root" output -json state_bucket_names)
  jq -e \
    --arg bootstrap "$TF_VAR_bootstrap_state_bucket_name" \
    --arg application "$TF_VAR_application_state_bucket_name" \
    '.bootstrap == $bootstrap and .application == $application' \
    <<<"$names" >/dev/null || fail "state bucket outputs do not match the requested project"
  "$TERRAFORM_BIN" -chdir="$state_bucket_root" plan \
    -input=false \
    -lock-timeout=60s \
    -detailed-exitcode
}

resolve_project_number() {
  actual_project_number=$(
    "$TERRAFORM_BIN" -chdir="$state_bucket_root" output -raw project_number
  )
  [[ $actual_project_number =~ ^[0-9]{6,20}$ ]] ||
    fail "Terraform could not resolve the project number"
  export TF_VAR_project_number=$actual_project_number
}

initialize_bootstrap() {
  "$TERRAFORM_BIN" -chdir="$terraform_root" init \
    -input=false \
    -reconfigure \
    -backend-config="bucket=$TF_VAR_bootstrap_state_bucket_name" \
    -backend-config="prefix=assessment/bootstrap"
}

initialize_application() {
  "$TERRAFORM_BIN" -chdir="$application_root" init \
    -input=false \
    -reconfigure \
    -backend-config="bucket=$TF_VAR_application_state_bucket_name" \
    -backend-config="prefix=assessment/dev"
}

verify_application_cost_controls() {
  local managed_services execution_plane
  initialize_application
  managed_services=$("$TERRAFORM_BIN" -chdir="$application_root" output -json managed_services)
  execution_plane=$("$TERRAFORM_BIN" -chdir="$application_root" output -json execution_plane)

  jq -e '
    .budget.enabled == true and
    .budget.currency_code == "EUR" and
    .budget.amount_units == "5" and
    .budget.threshold_rules == [0.2, 0.5, 0.8, 1]
  ' <<<"$managed_services" >/dev/null || fail "the deployed EUR 5 budget guard is missing"
  jq -e '
    .api_service.min_instances == 0 and
    .api_service.max_instances == 1 and
    .worker_service.min_instances == 0 and
    .worker_service.max_instances == 1
  ' <<<"$execution_plane" >/dev/null || fail "the deployed Cloud Run scale guard is missing"
}

select_runtime_policy_mode() {
  local state_resources
  state_resources=$("$TERRAFORM_BIN" -chdir="$terraform_root" state list) ||
    fail "Terraform could not read bootstrap state"
  if grep '^google_cloud_run_v2_service_iam_' <<<"$state_resources" >/dev/null; then
    export TF_VAR_enable_runtime_policy=true
  else
    export TF_VAR_enable_runtime_policy=false
  fi
}

plan_bootstrap() {
  local plan_file=$1
  "$TERRAFORM_BIN" -chdir="$terraform_root" plan \
    -input=false \
    -lock-timeout=60s \
    -out="$plan_file"
  "$TERRAFORM_BIN" -chdir="$terraform_root" show -no-color "$plan_file"
}

verify_bootstrap() {
  local queue secrets
  initialize_state_buckets
  verify_state_buckets
  resolve_project_number
  initialize_bootstrap
  select_runtime_policy_mode
  "$TERRAFORM_BIN" -chdir="$terraform_root" plan \
    -input=false \
    -lock-timeout=60s \
    -detailed-exitcode
  "$TERRAFORM_BIN" -chdir="$terraform_root" output -json github_delivery |
    jq -e --arg repository "$TF_VAR_github_repository" \
      '.repository == $repository
        and (.variables | length) >= 10
        and .branch_protection.admin_enforcement == true
        and .branch_protection.required_linear_history == true
        and .branch_protection.allows_deletions == false
        and .branch_protection.allows_force_pushes == false' >/dev/null
  "$TERRAFORM_BIN" -chdir="$terraform_root" output -json runtime_policy |
    jq -e --arg database "${TF_VAR_system_code}-${TF_VAR_environment}" \
      '.firestore_database_name == $database
        and .firestore_runtime_binding_count == 2
        and .firestore_index_binding_count == 1
        and .service_usage_binding_count == 2' >/dev/null
  queue=$("$TERRAFORM_BIN" -chdir="$terraform_root" output -json dispatch_queue)
  jq -e \
    --arg name "${TF_VAR_system_code}-${TF_VAR_environment}-run-dispatch" \
    --arg location "$TF_VAR_region" \
    '.name == $name and .location == $location
      and .max_dispatches_per_second == 1
      and .max_concurrent_dispatches == 1' <<<"$queue" >/dev/null ||
    fail "the Terraform-managed queue contract is invalid"
  secrets=$("$TERRAFORM_BIN" -chdir="$terraform_root" output -raw secret_version_count)
  [[ $secrets == 2 ]] || fail "Terraform must manage both initial secret versions"
  printf 'bootstrap verified for %s and %s\n' \
    "$TF_VAR_project_id" "$TF_VAR_github_repository"
}

apply_bootstrap_plan() {
  local plan_file=$1
  "$TERRAFORM_BIN" -chdir="$terraform_root" apply \
    -input=false \
    -lock-timeout=60s \
    "$plan_file"
}

wait_for_deploy_workflow() {
  local repository=$1 revision=$2 dispatch_id run_title run_id deadline
  dispatch_id=$(openssl rand -hex 16)
  run_title="sai-deploy-$dispatch_id"
  gh workflow run deploy.yml \
    --repo "$repository" \
    --ref master \
    -f "dispatch_id=$dispatch_id" \
    -f "expected_sha=$revision"
  deadline=$((SECONDS + 300))
  while ((SECONDS < deadline)); do
    run_id=$(gh run list \
      --repo "$repository" \
      --workflow deploy.yml \
      --branch master \
      --event workflow_dispatch \
      --limit 20 \
      --json databaseId,displayTitle,headSha \
      --jq "map(select(.displayTitle == \"$run_title\")) | first | .databaseId // empty")
    if [[ -n $run_id ]]; then
      gh run watch "$run_id" --repo "$repository" --exit-status
      printf 'deployment workflow %s completed for %s\n' "$run_id" "$revision"
      return
    fi
    sleep 5
  done
  fail "GitHub did not register the deployment workflow within five minutes"
}

main() {
  [[ $# -ge 4 && $# -le 5 ]] || { usage >&2; exit 2; }
  local command_name=$1 project_id=$2 repository=$3 reviewer=$4 region=${5:-europe-west3}
  [[ $command_name =~ ^(plan|apply|verify|deploy)$ ]] || { usage >&2; exit 2; }
  [[ $project_id =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || fail "invalid project id"
  [[ $repository =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "repository must use owner/name syntax"
  [[ $reviewer =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}$ ]] || fail "invalid reviewer login"
  [[ $region =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || fail "invalid region"

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
  application_root="$repository_root/task-03-deployment-strategy/terraform/environments/dev"
  state_bucket_root="$repository_root/task-03-deployment-strategy/terraform/state_bucket"
  [[ -f $terraform_root/main.tf ]] || fail "run this command from the repository checkout"
  [[ -f $state_bucket_root/main.tf ]] || fail "state bucket Terraform root is missing"
  [[ -f $application_root/main.tf ]] || fail "application Terraform root is missing"
  gh repo view "$repository" --json nameWithOwner --jq '.nameWithOwner' >/dev/null
  resolve_budget_coordinates

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
  export TF_VAR_bootstrap_state_bucket_name="${project_id}-sai-bootstrap-tf-state"
  export TF_VAR_application_state_bucket_name="${project_id}-sai-app-tf-state"
  export TF_VAR_existing_state_buckets='{}'
  if [[ ${GCP_IMPORT_STATE_BUCKETS:-false} == true ]]; then
    TF_VAR_existing_state_buckets=$(jq -cn \
      --arg bootstrap "$TF_VAR_bootstrap_state_bucket_name" \
      --arg application "$TF_VAR_application_state_bucket_name" \
      '{bootstrap: $bootstrap, application: $application}')
    export TF_VAR_existing_state_buckets
  fi
  export TF_VAR_enable_github_wif=true
  export TF_VAR_github_repository=$repository
  export TF_VAR_github_branch=master
  export TF_VAR_github_environment=gcp-dev
  export TF_VAR_github_reviewer=$reviewer
  export TF_VAR_seed_secret_versions=true
  export TF_VAR_enable_runtime_policy=false
  export TF_VAR_api_allow_unauthenticated=true
  export TF_VAR_secret_ids='{"api_key_pepper":"sai-dev-api-key-pepper","task_signing_hmac":"sai-dev-task-signing-hmac"}'
  export TF_VAR_billing_account_id=$GCP_BILLING_ACCOUNT_ID
  export TF_VAR_budget_notification_emails=$resolved_budget_notification_emails

  BOOTSTRAP_TEMP_DIR=$(mktemp -d -t sai-bootstrap.XXXXXX)
  trap cleanup EXIT

  if [[ $command_name == verify ]]; then
    verify_bootstrap
    exit 0
  fi

  initialize_state_buckets
  state_plan="$BOOTSTRAP_TEMP_DIR/state-buckets.tfplan"
  plan_state_buckets "$state_plan"
  if [[ $command_name == plan ]] && ! all_state_buckets_exist; then
    printf '%s\n' \
      "bootstrap plan is available after Terraform creates both isolated state buckets"
    exit 0
  fi
  if [[ $command_name != plan ]]; then
    "$TERRAFORM_BIN" -chdir="$state_bucket_root" apply \
      -input=false \
      -lock-timeout=60s \
      "$state_plan"
    all_state_buckets_exist || fail "Terraform did not create both state buckets"
  fi

  verify_state_buckets
  resolve_project_number
  initialize_bootstrap
  select_runtime_policy_mode
  plan_file="$BOOTSTRAP_TEMP_DIR/bootstrap.tfplan"
  plan_bootstrap "$plan_file"
  [[ $command_name != plan ]] || exit 0

  apply_bootstrap_plan "$plan_file"
  verify_bootstrap

  if [[ $command_name == deploy ]]; then
    local_revision=$(git -C "$repository_root" rev-parse HEAD)
    remote_revision=$(gh api "repos/$repository/commits/master" --jq '.sha')
    [[ $local_revision == "$remote_revision" ]] ||
      fail "push the current master revision before dispatching deployment"
    wait_for_deploy_workflow "$repository" "$local_revision"

    export TF_VAR_enable_runtime_policy=true
    post_deploy_plan="$BOOTSTRAP_TEMP_DIR/post-deploy-iam.tfplan"
    plan_bootstrap "$post_deploy_plan"
    apply_bootstrap_plan "$post_deploy_plan"
    verify_bootstrap
    verify_application_cost_controls
  fi
}

main "$@"
