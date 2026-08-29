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
  GCP_BILLING_ACCOUNT_ID           Defaults to the project's linked account
  GCP_BUDGET_NOTIFICATION_EMAILS   Defaults to the active gcloud user as a JSON array
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
  local project_id=$1 billing_name billing_currency active_account recipients

  if [[ -n ${GCP_BILLING_ACCOUNT_ID:-} ]]; then
    resolved_billing_account_id=$GCP_BILLING_ACCOUNT_ID
  else
    billing_name=$(gcloud billing projects describe "$project_id" \
      --format='value(billingAccountName)')
    resolved_billing_account_id=${billing_name#billingAccounts/}
  fi
  [[ $resolved_billing_account_id =~ ^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$ ]] ||
    fail "the project must have a readable linked billing account"
  billing_currency=$(gcloud billing accounts describe "$resolved_billing_account_id" \
    --format='value(currencyCode)')
  [[ $billing_currency == EUR ]] ||
    fail "the linked billing account must use EUR for the EUR 5 test budget"

  if [[ -n ${GCP_BUDGET_NOTIFICATION_EMAILS:-} ]]; then
    recipients=$GCP_BUDGET_NOTIFICATION_EMAILS
  else
    active_account=$(gcloud auth list \
      --filter='status:ACTIVE' \
      --format='value(account)' \
      --limit=1)
    [[ $active_account =~ ^[^@[:space:]]+@[^@[:space:]]+$ ]] ||
      fail "set GCP_BUDGET_NOTIFICATION_EMAILS to at least one monitored address"
    [[ $active_account != *.gserviceaccount.com ]] ||
      fail "a service account cannot receive budget email; set GCP_BUDGET_NOTIFICATION_EMAILS"
    recipients=$(jq -cn --arg address "$active_account" '[$address]')
  fi

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
  ' <<<"$recipients") || fail "GCP_BUDGET_NOTIFICATION_EMAILS is invalid"
}

cleanup() {
  [[ -z ${BOOTSTRAP_TEMP_DIR:-} ]] || rm -rf -- "$BOOTSTRAP_TEMP_DIR"
}

bucket_exists() {
  local bucket_name=$1 error_output
  if error_output=$(gcloud storage buckets describe "gs://$bucket_name" \
    --project "$TF_VAR_project_id" 2>&1); then
    return 0
  fi
  if [[ -z $error_output ]] || grep -Eiq 'not[ -]?found|does not exist|404' <<<"$error_output"; then
    return 1
  fi
  printf '%s\n' "$error_output" >&2
  fail "could not inspect state bucket $bucket_name"
}

object_exists() {
  local object_url=$1 error_output
  if error_output=$(gcloud storage objects describe "$object_url" \
    --project "$TF_VAR_project_id" 2>&1); then
    return 0
  fi
  if [[ -z $error_output ]] ||
    grep -Eiq 'not[ -]?found|does not exist|matched no|404' <<<"$error_output"; then
    return 1
  fi
  printf '%s\n' "$error_output" >&2
  fail "could not inspect remote state object $object_url"
}

legacy_remote_state_needs_migration() {
  local prefix legacy_url target_url target_bucket
  for prefix in assessment/bootstrap assessment/dev; do
    if [[ $prefix == assessment/bootstrap ]]; then
      target_bucket=$TF_VAR_bootstrap_state_bucket_name
    else
      target_bucket=$TF_VAR_application_state_bucket_name
    fi
    legacy_url="gs://$legacy_state_bucket_name/$prefix/default.tfstate"
    target_url="gs://$target_bucket/$prefix/default.tfstate"
    if object_exists "$legacy_url" && ! object_exists "$target_url"; then
      return 0
    fi
  done
  return 1
}

migrate_backend_if_needed() {
  local terraform_directory=$1 prefix=$2 target_bucket=$3
  local legacy_url="gs://$legacy_state_bucket_name/$prefix/default.tfstate"
  local target_url="gs://$target_bucket/$prefix/default.tfstate"
  object_exists "$legacy_url" || return 0
  object_exists "$target_url" && return 0

  "$TERRAFORM_BIN" -chdir="$terraform_directory" init \
    -input=false \
    -reconfigure \
    -backend-config="bucket=$legacy_state_bucket_name" \
    -backend-config="prefix=$prefix"
  "$TERRAFORM_BIN" -chdir="$terraform_directory" init \
    -input=false \
    -migrate-state \
    -force-copy \
    -backend-config="bucket=$target_bucket" \
    -backend-config="prefix=$prefix"
  object_exists "$target_url" || fail "Terraform did not migrate $prefix state"
}

migrate_legacy_remote_state() {
  migrate_backend_if_needed \
    "$terraform_root" \
    assessment/bootstrap \
    "$TF_VAR_bootstrap_state_bucket_name"
  migrate_backend_if_needed \
    "$application_root" \
    assessment/dev \
    "$TF_VAR_application_state_bucket_name"
}

discover_existing_state_buckets() {
  local existing='{}' scope bucket_name
  for scope in bootstrap application; do
    if [[ $scope == bootstrap ]]; then
      bucket_name=$TF_VAR_bootstrap_state_bucket_name
    else
      bucket_name=$TF_VAR_application_state_bucket_name
    fi
    if bucket_exists "$bucket_name"; then
      existing=$(jq -cn \
        --argjson current "$existing" \
        --arg scope "$scope" \
        --arg bucket "$bucket_name" \
        '$current + {($scope): $bucket}')
    fi
  done
  export TF_VAR_existing_state_buckets=$existing
}

all_state_buckets_exist() {
  bucket_exists "$TF_VAR_bootstrap_state_bucket_name" &&
    bucket_exists "$TF_VAR_application_state_bucket_name"
}

initialize_state_buckets() {
  local state_directory="$repository_root/.local/terraform"
  mkdir -p "$state_directory"
  "$TERRAFORM_BIN" -chdir="$state_bucket_root" init \
    -input=false \
    -reconfigure \
    -backend-config="path=$state_directory/${TF_VAR_project_id}-state-buckets.tfstate"
  discover_existing_state_buckets
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
  all_state_buckets_exist || fail "both isolated Terraform state buckets must exist"
  "$TERRAFORM_BIN" -chdir="$state_bucket_root" plan \
    -input=false \
    -lock-timeout=60s \
    -detailed-exitcode
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

cloud_run_service_exists() {
  local service_name=$1
  gcloud run services describe "$service_name" \
    --project "$TF_VAR_project_id" \
    --region "$TF_VAR_region" >/dev/null 2>&1
}

select_runtime_policy_mode() {
  if "$TERRAFORM_BIN" -chdir="$terraform_root" state list 2>/dev/null |
    grep -q '^google_cloud_run_v2_service_iam_'; then
    export TF_VAR_enable_runtime_policy=true
    return
  fi

  local api_exists=false worker_exists=false
  if cloud_run_service_exists "${TF_VAR_system_code}-${TF_VAR_environment}-api"; then
    api_exists=true
  fi
  if cloud_run_service_exists "${TF_VAR_system_code}-${TF_VAR_environment}-worker"; then
    worker_exists=true
  fi
  [[ $api_exists == "$worker_exists" ]] ||
    fail "only one expected Cloud Run service exists; repair the application deployment first"
  export TF_VAR_enable_runtime_policy=$api_exists
}

plan_bootstrap() {
  local plan_file=$1
  "$TERRAFORM_BIN" -chdir="$terraform_root" plan \
    -input=false \
    -lock-timeout=60s \
    -out="$plan_file"
  "$TERRAFORM_BIN" -chdir="$terraform_root" show -no-color "$plan_file"
}

enabled_secret_version_exists() {
  local secret_id=$1 version
  version=$(gcloud secrets versions list "$secret_id" \
    --project "$TF_VAR_project_id" \
    --filter='state=ENABLED' \
    --limit=1 \
    --format='value(name)')
  [[ -n $version ]]
}

repair_secret_versions() {
  local key secret_id repair_plan
  local -a replace_args=()
  for key in api_key_pepper task_signing_hmac; do
    secret_id=$(jq -er --arg key "$key" '.[$key]' <<<"$TF_VAR_secret_ids")
    if ! enabled_secret_version_exists "$secret_id"; then
      replace_args+=("-replace=terraform_data.secret_version[\"$key\"]")
    fi
  done
  [[ ${#replace_args[@]} -gt 0 ]] || return

  repair_plan="$BOOTSTRAP_TEMP_DIR/secret-repair.tfplan"
  "$TERRAFORM_BIN" -chdir="$terraform_root" plan \
    -input=false \
    -lock-timeout=60s \
    "${replace_args[@]}" \
    -out="$repair_plan"
  "$TERRAFORM_BIN" -chdir="$terraform_root" show -no-color "$repair_plan"
  "$TERRAFORM_BIN" -chdir="$terraform_root" apply \
    -input=false \
    -lock-timeout=60s \
    "$repair_plan"
}

verify_secret_versions() {
  local key secret_id
  for key in api_key_pepper task_signing_hmac; do
    secret_id=$(jq -er --arg key "$key" '.[$key]' <<<"$TF_VAR_secret_ids")
    enabled_secret_version_exists "$secret_id" ||
      fail "secret $secret_id has no enabled version; rerun bootstrap.sh apply"
  done
}

verify_bootstrap() {
  initialize_state_buckets
  verify_state_buckets
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
  verify_secret_versions
  gcloud tasks queues describe "${TF_VAR_system_code}-${TF_VAR_environment}-run-dispatch" \
    --project "$TF_VAR_project_id" \
    --location "$TF_VAR_region" >/dev/null
  printf 'bootstrap verified for %s and %s\n' \
    "$TF_VAR_project_id" "$TF_VAR_github_repository"
}

apply_bootstrap_plan() {
  local plan_file=$1
  "$TERRAFORM_BIN" -chdir="$terraform_root" apply \
    -input=false \
    -lock-timeout=60s \
    "$plan_file"
  repair_secret_versions
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
  application_root="$repository_root/task-03-deployment-strategy/terraform/environments/dev"
  state_bucket_root="$repository_root/task-03-deployment-strategy/terraform/state_bucket"
  [[ -f $terraform_root/main.tf ]] || fail "run this command from the repository checkout"
  [[ -f $state_bucket_root/main.tf ]] || fail "state bucket Terraform root is missing"
  [[ -f $application_root/main.tf ]] || fail "application Terraform root is missing"
  actual_project_number=$(
    gcloud projects describe "$project_id" --format='value(projectNumber)'
  )
  [[ $actual_project_number =~ ^[0-9]{6,20}$ ]] || fail "could not resolve the project number"
  gh repo view "$repository" --json nameWithOwner --jq '.nameWithOwner' >/dev/null
  resolve_budget_coordinates "$project_id"

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
  legacy_state_bucket_name="${project_id}-sai-tf-state"
  export TF_VAR_enable_github_wif=true
  export TF_VAR_github_repository=$repository
  export TF_VAR_github_branch=master
  export TF_VAR_github_environment=gcp-dev
  export TF_VAR_github_reviewer=$reviewer
  export TF_VAR_seed_secret_versions=true
  export TF_VAR_enable_runtime_policy=false
  export TF_VAR_api_allow_unauthenticated=true
  export TF_VAR_secret_ids='{"api_key_pepper":"sai-dev-api-key-pepper","task_signing_hmac":"sai-dev-task-signing-hmac"}'
  export TF_VAR_billing_account_id=$resolved_billing_account_id
  export TF_VAR_budget_notification_emails=$resolved_budget_notification_emails

  BOOTSTRAP_TEMP_DIR=$(mktemp -d -t sai-bootstrap.XXXXXX)
  trap cleanup EXIT

  if [[ $command_name == verify ]]; then
    legacy_remote_state_needs_migration &&
      fail "legacy remote state still needs migration; run bootstrap.sh apply"
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
    migrate_legacy_remote_state
  elif legacy_remote_state_needs_migration; then
    fail "legacy remote state needs migration; run bootstrap.sh apply before planning"
  fi

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
