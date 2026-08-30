#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: migrate_firestore_index_state.sh TERRAFORM_ROOT PROJECT_ID DATABASE_ID

Safely aligns legacy Firestore index addresses before the protected Terraform plan.
DATABASE_ID must be sai-dev.
EOF
}

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

extract_string_attribute() {
  local state=$1 attribute=$2 address=$3 values
  values=$(sed -nE \
    "s/^[[:space:]]*${attribute}[[:space:]]*=[[:space:]]*\"([^\"]+)\"[[:space:]]*$/\\1/p" \
    <<<"$state")
  [[ -n $values && $values != *$'\n'* ]] ||
    fail "could not read one $attribute value from $address"
  printf '%s\n' "$values"
}

show_state_resource() {
  local address=$1
  "$TERRAFORM_BIN" -chdir="$terraform_root" state show -no-color "$address"
}

state_has_address() {
  local address=$1
  grep -Fqx -- "$address" <<<"$state_addresses"
}

[[ $# -eq 3 ]] || {
  usage >&2
  exit 2
}

terraform_root=$1
project_id=$2
database_id=$3
TERRAFORM_BIN=${TERRAFORM_BIN:-terraform}

[[ $terraform_root == /* ]] || fail "TERRAFORM_ROOT must be an absolute path"
[[ -f $terraform_root/main.tf ]] || fail "TERRAFORM_ROOT must contain main.tf"
[[ $project_id =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || fail "PROJECT_ID is invalid"
[[ $database_id == sai-dev ]] || fail "DATABASE_ID must be sai-dev"
command -v "$TERRAFORM_BIN" >/dev/null 2>&1 || fail "$TERRAFORM_BIN is required"

set +e
state_addresses=$("$TERRAFORM_BIN" -chdir="$terraform_root" state list 2>&1)
state_status=$?
set -e
if ((state_status != 0)); then
  if [[ $state_addresses == *"No state file was found"* ]]; then
    state_addresses=""
  else
    printf '%s\n' "$state_addresses" >&2
    fail "could not inspect Terraform state"
  fi
fi

legacy_names=(
  sessions
  runs
  run_events
  audit_entries
  quota_execution_leases_active
  quota_sse_leases_active
)
forget_addresses=("")
move_sources=("")
move_destinations=("")

for legacy_name in "${legacy_names[@]}"; do
  legacy_address="module.managed_services.google_firestore_index.${legacy_name}"
  replacement_address="module.managed_services.google_firestore_index.assessment_${legacy_name}"
  legacy_in_named_database=false

  if state_has_address "$legacy_address"; then
    legacy_state=$(show_state_resource "$legacy_address")
    legacy_project=$(extract_string_attribute "$legacy_state" project "$legacy_address")
    legacy_database=$(extract_string_attribute "$legacy_state" database "$legacy_address")
    [[ $legacy_project == "$project_id" ]] ||
      fail "$legacy_address belongs to project $legacy_project, not $project_id"

    case $legacy_database in
      '(default)')
        forget_addresses+=("$legacy_address")
        ;;
      "$database_id")
        legacy_in_named_database=true
        ;;
      *)
        fail "$legacy_address belongs to unexpected database $legacy_database"
        ;;
    esac
  fi

  if state_has_address "$replacement_address"; then
    replacement_state=$(show_state_resource "$replacement_address")
    replacement_project=$(extract_string_attribute \
      "$replacement_state" project "$replacement_address")
    replacement_database=$(extract_string_attribute \
      "$replacement_state" database "$replacement_address")
    [[ $replacement_project == "$project_id" ]] ||
      fail "$replacement_address belongs to project $replacement_project, not $project_id"
    [[ $replacement_database == "$database_id" ]] ||
      fail "$replacement_address belongs to unexpected database $replacement_database"
    [[ $legacy_in_named_database == false ]] ||
      fail "$legacy_address and $replacement_address both own a $database_id index"

    move_sources+=("$replacement_address")
    move_destinations+=("$legacy_address")
  fi
done

for ((index = 1; index < ${#forget_addresses[@]}; index++)); do
  "$TERRAFORM_BIN" -chdir="$terraform_root" state rm \
    -lock-timeout=60s "${forget_addresses[$index]}"
done

for ((index = 1; index < ${#move_sources[@]}; index++)); do
  "$TERRAFORM_BIN" -chdir="$terraform_root" state mv \
    -lock-timeout=60s "${move_sources[$index]}" "${move_destinations[$index]}"
done

printf 'Firestore index state is ready for database %s.\n' "$database_id"
