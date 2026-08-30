#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: model_plane.sh plan|apply|verify TFVARS_FILE STATE_BUCKET

Required for apply:
  MODEL_PLANE_COST_ACKNOWLEDGEMENT=I_ACCEPT_GPU_COSTS

Optional:
  TERRAFORM_BIN  Defaults to terraform

This script never calls gcloud. The caller must provide Application Default
Credentials or a workload-identity credential through the standard Google
provider environment.
EOF
}

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

[[ $# -eq 3 ]] || { usage >&2; exit 2; }
command_name=$1
tfvars_file=$2
state_bucket=$3

[[ $command_name =~ ^(plan|apply|verify)$ ]] || { usage >&2; exit 2; }
[[ -f $tfvars_file ]] || fail "TFVARS_FILE does not exist"
[[ $state_bucket =~ ^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$ ]] || fail "STATE_BUCKET is invalid"

TERRAFORM_BIN=${TERRAFORM_BIN:-terraform}
command -v "$TERRAFORM_BIN" >/dev/null 2>&1 || fail "$TERRAFORM_BIN is required"

script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
terraform_root="$script_directory/../terraform/environments/production-model-plane"
tfvars_file=$(cd -- "$(dirname -- "$tfvars_file")" && pwd)/$(basename -- "$tfvars_file")
plan_directory=$(mktemp -d "${TMPDIR:-/tmp}/sai-model-plane.XXXXXX")
trap 'rm -rf -- "$plan_directory"' EXIT
plan_file="$plan_directory/model-plane.tfplan"

"$TERRAFORM_BIN" -chdir="$terraform_root" init \
  -input=false \
  -reconfigure \
  -backend-config="bucket=$state_bucket" \
  -backend-config="prefix=production/model-plane"

case "$command_name" in
  plan)
    "$TERRAFORM_BIN" -chdir="$terraform_root" plan \
      -input=false \
      -lock-timeout=60s \
      -var-file="$tfvars_file"
    ;;
  apply)
    [[ ${MODEL_PLANE_COST_ACKNOWLEDGEMENT:-} == I_ACCEPT_GPU_COSTS ]] ||
      fail "set MODEL_PLANE_COST_ACKNOWLEDGEMENT=I_ACCEPT_GPU_COSTS after cost, quota, license, and data review"
    "$TERRAFORM_BIN" -chdir="$terraform_root" plan \
      -input=false \
      -lock-timeout=60s \
      -var-file="$tfvars_file" \
      -out="$plan_file"
    "$TERRAFORM_BIN" -chdir="$terraform_root" show -no-color "$plan_file"
    "$TERRAFORM_BIN" -chdir="$terraform_root" apply \
      -input=false \
      -lock-timeout=60s \
      "$plan_file"
    ;;
  verify)
    "$TERRAFORM_BIN" -chdir="$terraform_root" plan \
      -input=false \
      -lock-timeout=60s \
      -detailed-exitcode \
      -var-file="$tfvars_file"
    "$TERRAFORM_BIN" -chdir="$terraform_root" output -json model_plane
    ;;
esac
