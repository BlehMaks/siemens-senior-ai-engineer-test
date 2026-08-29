#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

[[ ${GCP_PROJECT_ID:-} =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] ||
  fail "GCP_PROJECT_ID is invalid"
[[ ${GCP_SECRET_ID:-} =~ ^[a-z][a-z0-9-]{2,254}$ ]] ||
  fail "GCP_SECRET_ID is invalid"
command -v gcloud >/dev/null 2>&1 || fail "gcloud is required"
command -v openssl >/dev/null 2>&1 || fail "openssl is required"

existing_version=$(
  gcloud secrets versions list "$GCP_SECRET_ID" \
    --project "$GCP_PROJECT_ID" \
    --filter='state=ENABLED' \
    --limit=1 \
    --format='value(name)'
)
if [[ -n $existing_version ]]; then
  printf 'secret %s already has an enabled version\n' "$GCP_SECRET_ID"
  exit 0
fi

openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n' |
  gcloud secrets versions add "$GCP_SECRET_ID" \
    --project "$GCP_PROJECT_ID" \
    --data-file=- >/dev/null
printf 'created the initial version for secret %s\n' "$GCP_SECRET_ID"
