#!/usr/bin/env bash
set -euo pipefail

CLOUD_SMOKE_TEMP_DIR=""
CLOUD_SMOKE_API_KEY_A=""
CLOUD_SMOKE_API_KEY_B=""
CLOUD_SMOKE_PEPPER=""
CLOUD_SMOKE_PROJECT=""
CLOUD_SMOKE_DATABASE=""
CLOUD_SMOKE_UV_BIN=""
CLOUD_SMOKE_KEY_FILE_A=""
CLOUD_SMOKE_KEY_FILE_B=""

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  cloud_api_smoke.sh PROJECT REGION ENVIRONMENT PROJECT_NUMBER SMOKE_ID
EOF
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required"
}

canonical_directory() {
  local path=$1 parent marker=$'\034' resolved
  if [[ $path == */* ]]; then
    parent=${path%/*}
    [[ -n $parent ]] || parent=/
  else
    parent=.
  fi
  if ! resolved=$(cd -P -- "$parent" && pwd -P && printf '%s' "$marker"); then
    fail "could not resolve the smoke script directory"
  fi
  resolved=${resolved%$marker}
  resolved=${resolved%$'\n'}
  printf '%s' "$resolved"
}

resolve_script_directory() {
  local script_path=$1 script_dir target marker=$'\034' hops=0
  while [[ -h $script_path ]]; do
    hops=$((hops + 1))
    ((hops <= 40)) || fail "smoke script symlink chain is too long"
    script_dir=$(canonical_directory "$script_path")
    if ! target=$(readlink -n "$script_path" && printf '%s' "$marker"); then
      fail "could not read the smoke script symlink"
    fi
    target=${target%$marker}
    [[ -n $target ]] || fail "smoke script symlink target is empty"
    if [[ $target == /* ]]; then
      script_path=$target
    else
      script_path=$script_dir/$target
    fi
  done
  [[ -f $script_path ]] || fail "resolved smoke script is not a regular file"
  canonical_directory "$script_path"
}

revoke_key() {
  local key=$1
  [[ -n $key ]] || return 0
  AGENT_API_KEY_PEPPER="$CLOUD_SMOKE_PEPPER" \
  AGENT_API_AUTHORIZATION="Bearer $key" \
    "$CLOUD_SMOKE_UV_BIN" run --frozen --all-packages \
    agent-api-key-admin \
    --gcp-project "$CLOUD_SMOKE_PROJECT" \
    --firestore-database "$CLOUD_SMOKE_DATABASE" \
    revoke >/dev/null
}

load_key_file() {
  local path=$1 variable=$2 value=""
  [[ -f $path ]] || return 1
  IFS= read -r value <"$path" || return 1
  [[ -n $value ]] || return 1
  printf -v "$variable" '%s' "$value"
}

cleanup() {
  local status=$? cleanup_failed=0
  trap - EXIT
  set +e
  if [[ -z $CLOUD_SMOKE_API_KEY_A && -f $CLOUD_SMOKE_KEY_FILE_A ]] &&
    ! load_key_file "$CLOUD_SMOKE_KEY_FILE_A" CLOUD_SMOKE_API_KEY_A; then
    cleanup_failed=1
  fi
  if [[ -z $CLOUD_SMOKE_API_KEY_B && -f $CLOUD_SMOKE_KEY_FILE_B ]] &&
    ! load_key_file "$CLOUD_SMOKE_KEY_FILE_B" CLOUD_SMOKE_API_KEY_B; then
    cleanup_failed=1
  fi
  if ! revoke_key "$CLOUD_SMOKE_API_KEY_A"; then
    cleanup_failed=1
  fi
  if ! revoke_key "$CLOUD_SMOKE_API_KEY_B"; then
    cleanup_failed=1
  fi
  CLOUD_SMOKE_API_KEY_A=""
  CLOUD_SMOKE_API_KEY_B=""
  CLOUD_SMOKE_PEPPER=""
  [[ -z ${CLOUD_SMOKE_TEMP_DIR:-} ]] || rm -rf -- "$CLOUD_SMOKE_TEMP_DIR"
  if ((status == 0 && cleanup_failed != 0)); then
    printf 'error: smoke keys could not be revoked\n' >&2
    status=1
  elif ((cleanup_failed != 0)); then
    printf 'warning: smoke failed and one or more keys could not be revoked\n' >&2
  fi
  exit "$status"
}

main() {
  [[ $# -eq 5 ]] || { usage >&2; exit 2; }
  local project=$1 region=$2 environment=$3 project_number=$4 smoke_id=$5
  local actual_number api_url invocation_path repository_root script_dir verified_dir

  [[ $project =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || fail "invalid project id"
  [[ $region =~ ^[a-z]+-[a-z]+[0-9]$ ]] || fail "invalid region"
  [[ $environment == dev ]] || fail "only the reviewed dev environment is supported"
  [[ $project_number =~ ^[0-9]{6,20}$ ]] || fail "invalid project number"
  [[ $smoke_id =~ ^[a-z0-9][a-z0-9-]{2,31}$ ]] || fail "SMOKE_ID must be a short lowercase opaque label"

  CLOUD_SMOKE_UV_BIN=${UV_BIN:-uv}
  for tool in gcloud jq curl readlink "$CLOUD_SMOKE_UV_BIN"; do
    require_tool "$tool"
  done

  invocation_path=${BASH_SOURCE[0]}
  script_dir=$(resolve_script_directory "$invocation_path")
  repository_root=$(cd "$script_dir/../.." && pwd -P)
  [[ -x $script_dir/api_smoke.sh ]] || fail "reviewed API smoke script is missing"
  cd "$repository_root"

  actual_number=$(gcloud projects describe "$project" --format='value(projectNumber)')
  [[ $actual_number == "$project_number" ]] || fail "project number does not match"
  verified_dir=$(resolve_script_directory "$invocation_path")
  [[ $verified_dir == "$script_dir" ]] || fail "smoke script path changed during startup"

  api_url=$(gcloud run services describe "sai-$environment-api" \
    --project "$project" \
    --region "$region" \
    --format='value(status.url)')
  [[ $api_url == https://*.run.app ]] || fail "Cloud Run API URL is missing or unexpected"

  CLOUD_SMOKE_PROJECT=$project
  CLOUD_SMOKE_DATABASE="sai-$environment"
  CLOUD_SMOKE_TEMP_DIR=$(mktemp -d -t sai-cloud-smoke.XXXXXX)
  CLOUD_SMOKE_KEY_FILE_A="$CLOUD_SMOKE_TEMP_DIR/key-a"
  CLOUD_SMOKE_KEY_FILE_B="$CLOUD_SMOKE_TEMP_DIR/key-b"
  trap cleanup EXIT
  export UV_PROJECT_ENVIRONMENT="$CLOUD_SMOKE_TEMP_DIR/venv"
  "$CLOUD_SMOKE_UV_BIN" sync --locked --all-packages --no-dev

  # Disable tracing before any secret or plaintext API key enters the shell.
  set +x
  CLOUD_SMOKE_PEPPER=$(gcloud secrets versions access latest \
    --secret "sai-$environment-api-key-pepper" \
    --project "$project")
  [[ -n $CLOUD_SMOKE_PEPPER ]] || fail "API key pepper secret is empty"

  AGENT_API_KEY_PEPPER="$CLOUD_SMOKE_PEPPER" \
    "$CLOUD_SMOKE_UV_BIN" run --frozen --all-packages \
    agent-api-key-admin \
    --gcp-project "$project" \
    --firestore-database "$CLOUD_SMOKE_DATABASE" \
    create \
    --tenant-id "smoke-$smoke_id-a" \
    --scope sessions:read --scope sessions:write \
    --scope runs:read --scope runs:write \
    --ttl-seconds 900 \
    --output-file "$CLOUD_SMOKE_KEY_FILE_A"
  load_key_file "$CLOUD_SMOKE_KEY_FILE_A" CLOUD_SMOKE_API_KEY_A ||
    fail "protected smoke key file is unreadable"

  AGENT_API_KEY_PEPPER="$CLOUD_SMOKE_PEPPER" \
    "$CLOUD_SMOKE_UV_BIN" run --frozen --all-packages \
    agent-api-key-admin \
    --gcp-project "$project" \
    --firestore-database "$CLOUD_SMOKE_DATABASE" \
    create \
    --tenant-id "smoke-$smoke_id-b" \
    --scope sessions:read --scope sessions:write \
    --scope runs:read --scope runs:write \
    --ttl-seconds 900 \
    --output-file "$CLOUD_SMOKE_KEY_FILE_B"
  load_key_file "$CLOUD_SMOKE_KEY_FILE_B" CLOUD_SMOKE_API_KEY_B ||
    fail "protected smoke key file is unreadable"
  [[ -n $CLOUD_SMOKE_API_KEY_A && -n $CLOUD_SMOKE_API_KEY_B ]] ||
    fail "smoke key creation returned an empty value"

  SMOKE_API_KEY_A="$CLOUD_SMOKE_API_KEY_A" \
  SMOKE_API_KEY_B="$CLOUD_SMOKE_API_KEY_B" \
    "$repository_root/task-03-deployment-strategy/scripts/api_smoke.sh" \
    "$api_url" "$smoke_id"

  revoke_key "$CLOUD_SMOKE_API_KEY_A"
  CLOUD_SMOKE_API_KEY_A=""
  rm -f -- "$CLOUD_SMOKE_KEY_FILE_A"
  revoke_key "$CLOUD_SMOKE_API_KEY_B"
  CLOUD_SMOKE_API_KEY_B=""
  rm -f -- "$CLOUD_SMOKE_KEY_FILE_B"
  CLOUD_SMOKE_PEPPER=""
  printf 'cloud API smoke passed and temporary keys were revoked\n'
}

main "$@"
