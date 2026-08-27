#!/usr/bin/env bash
set -euo pipefail

GCP_OPS_TEMP_FILE=""

usage() {
  cat <<'EOF'
Usage:
  gcp_ops.sh preflight PROJECT REGION ENVIRONMENT PROJECT_NUMBER BILLING_ACCOUNT
  gcp_ops.sh rollback PROJECT REGION ENVIRONMENT PROJECT_NUMBER SERVICE REVISION
  gcp_ops.sh teardown PROJECT REGION ENVIRONMENT PROJECT_NUMBER SYSTEM_CODE TERRAFORM_ROOT CONFIRMATION
EOF
}

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required"
}

cleanup_temp_file() {
  # A function trap keeps the generated path quoted as data instead of reparsing it.
  [[ -z ${GCP_OPS_TEMP_FILE:-} ]] || rm -f -- "$GCP_OPS_TEMP_FILE"
}

assert_gcloud_resource_absent() {
  local resource=$1 output exit_code=0
  shift

  output=$("$@" 2>&1) || exit_code=$?
  if ((exit_code == 0)); then
    fail "$resource remains after teardown"
  fi
  grep -Eq '(^|[^A-Z_])NOT_FOUND([^A-Z_]|$)|[Cc]ould not be found|[Dd]oes not exist' <<<"$output" ||
    fail "could not verify absence of $resource"
}

validate_target() {
  local project=$1 region=$2 environment=$3 expected_number=$4 actual_number
  [[ $project =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || fail "invalid project id"
  [[ $region =~ ^[a-z]+-[a-z]+[0-9]$ ]] || fail "invalid region"
  [[ $environment == dev ]] || fail "only the reviewed dev environment is supported"
  [[ $expected_number =~ ^[0-9]{6,20}$ ]] || fail "invalid project number"

  actual_number=$(gcloud projects describe "$project" --format='value(projectNumber)')
  [[ $actual_number == "$expected_number" ]] || fail "project number does not match"
}

preflight() {
  [[ $# -eq 5 ]] || { usage >&2; exit 2; }
  local project=$1 region=$2 environment=$3 project_number=$4 billing_account=$5
  local account enabled_services budgets required

  [[ $billing_account =~ ^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$ ]] || fail "invalid billing account"
  validate_target "$project" "$region" "$environment" "$project_number"

  account=$(gcloud auth list --filter='status:ACTIVE' --format='value(account)' --limit=1)
  [[ -n $account ]] || fail "no active gcloud identity"
  enabled_services=$(gcloud services list --enabled --project "$project" --format='value(config.name)')
  for required in run.googleapis.com cloudtasks.googleapis.com firestore.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com; do
    grep -Fqx "$required" <<<"$enabled_services" || fail "$required is not enabled"
  done
  budgets=$(gcloud billing budgets list \
    --billing-account "$billing_account" \
    --filter="budgetFilter.projects:projects/$project_number" \
    --format='value(displayName)' \
    --limit=1)
  [[ -n $budgets ]] || fail "no project-scoped billing budget"

  gcloud run services list --project "$project" --region "$region" --format='value(metadata.name)'
  gcloud tasks queues list --project "$project" --location "$region" --format='value(name)'
  printf 'preflight passed for %s/%s as %s\n' "$project" "$environment" "$account"
}

rollback() {
  [[ $# -eq 6 ]] || { usage >&2; exit 2; }
  local project=$1 region=$2 environment=$3 project_number=$4 service=$5 revision=$6
  local target_json before_json after_json

  [[ $service =~ ^[a-z][a-z0-9-]{1,62}$ ]] || fail "invalid service name"
  [[ $revision =~ ^[a-z][a-z0-9-]{1,62}$ ]] || fail "invalid revision name"
  validate_target "$project" "$region" "$environment" "$project_number"

  before_json=$(gcloud run services describe "$service" --project "$project" --region "$region" --format=json)
  target_json=$(gcloud run revisions describe "$revision" --project "$project" --region "$region" --format=json)
  jq -e --arg service "$service" '
    .metadata.labels["serving.knative.dev/service"] == $service and
    any(.status.conditions[]?; .type == "Ready" and .status == "True")
  ' <<<"$target_json" >/dev/null || fail "target revision is not a ready revision of the service"

  printf '%s\n' "$before_json" | jq '{service: .metadata.name, traffic: .status.traffic}'
  gcloud run services update-traffic "$service" \
    --project "$project" \
    --region "$region" \
    --to-revisions "$revision=100" \
    --quiet
  after_json=$(gcloud run services describe "$service" --project "$project" --region "$region" --format=json)
  jq -e --arg revision "$revision" '
    any(.status.traffic[]?; (.revisionName // .revision) == $revision and .percent == 100)
  ' <<<"$after_json" >/dev/null || fail "traffic verification failed"
  printf 'rollback verified: %s -> %s (100%%)\n' "$service" "$revision"
}

teardown() {
  [[ $# -eq 7 ]] || { usage >&2; exit 2; }
  local project=$1 region=$2 environment=$3 project_number=$4 system_code=$5 terraform_root=$6 confirmation=$7
  local plan_file plan_json service queue

  [[ $system_code =~ ^[a-z][a-z0-9-]{1,18}$ ]] || fail "invalid system code"
  [[ $terraform_root == /* && -f $terraform_root/main.tf ]] || fail "Terraform root must be an absolute initialized environment root"
  [[ $confirmation == "DESTROY:$project:$environment" ]] || fail "explicit teardown confirmation does not match"
  validate_target "$project" "$region" "$environment" "$project_number"

  plan_file=$(mktemp -t "${system_code}-${environment}-destroy.XXXXXX")
  GCP_OPS_TEMP_FILE=$plan_file
  trap cleanup_temp_file EXIT
  terraform -chdir="$terraform_root" plan -destroy -input=false -lock-timeout=60s -out="$plan_file"
  plan_json=$(terraform -chdir="$terraform_root" show -json "$plan_file")
  jq -e \
    --arg project "$project" \
    --arg project_number "$project_number" \
    --arg region "$region" \
    --arg environment "$environment" \
    --arg system_code "$system_code" \
    'def deletes: (.change.actions // []) | index("delete") != null;
     def expected_module:
       (.address | startswith("module.managed_services.") or startswith("module.run_services."));
     def expected_project:
       if .type == "google_billing_budget" then
         ([.change.before.budget_filter[]?.projects[]?] | length) > 0 and
         all(.change.before.budget_filter[]?.projects[]?; . == "projects/" + $project_number)
       elif .type == "google_service_account_iam_member" then
         (.change.before.service_account_id? // "") as $service_account |
         ($service_account | startswith("projects/" + $project + "/serviceAccounts/")) and
         ($service_account | endswith("@" + $project + ".iam.gserviceaccount.com"))
       else
         ((.change.before.project? // .change.before.project_id? // "") | tostring) == $project
       end;
     def expected_region:
       ((.change.before.location? // .change.before.location_id? // $region) | tostring) == $region;
     def expected_labels:
       (.change.before.labels? // {}) as $labels |
       ($labels.environment? // $environment) == $environment and
       ($labels.system? // $system_code) == $system_code;
     (.variables.project_id.value | tostring) == $project and
     (.variables.project_number.value | tostring) == $project_number and
     (.variables.region.value | tostring) == $region and
     (.variables.system_code.value | tostring) == $system_code and
     all(.resource_changes[]? | select(deletes);
       expected_module and expected_project and expected_region and expected_labels)' \
    <<<"$plan_json" >/dev/null || fail "destroy plan does not match the confirmed target"
  terraform -chdir="$terraform_root" show -no-color "$plan_file"
  terraform -chdir="$terraform_root" apply -input=false -lock-timeout=60s "$plan_file"

  for service in "$system_code-$environment-api" "$system_code-$environment-worker"; do
    assert_gcloud_resource_absent "Cloud Run service: $service" \
      gcloud run services describe "$service" --project "$project" --region "$region"
  done
  queue="$system_code-$environment-run-dispatch"
  assert_gcloud_resource_absent "Cloud Tasks queue: $queue" \
    gcloud tasks queues describe "$queue" --project "$project" --location "$region"
  printf 'runtime teardown verified for %s/%s; review retained bootstrap and data resources separately\n' "$project" "$environment"
}

main() {
  require_tool gcloud
  require_tool jq
  case ${1:-} in
    preflight)
      shift
      preflight "$@"
      ;;
    rollback)
      shift
      rollback "$@"
      ;;
    teardown)
      shift
      require_tool terraform
      teardown "$@"
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
