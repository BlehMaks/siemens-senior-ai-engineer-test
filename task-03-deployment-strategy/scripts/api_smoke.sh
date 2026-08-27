#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

expect_status() {
  local expected=$1 actual=$2 operation=$3
  [[ $actual == "$expected" ]] || fail "$operation returned HTTP $actual, expected $expected"
}

request_status() {
  local output=$1
  shift
  curl --silent --show-error --output "$output" --write-out '%{http_code}' "$@"
}

main() {
  [[ $# -eq 2 ]] || fail "usage: api_smoke.sh BASE_URL SMOKE_ID"
  local base_url=${1%/} smoke_id=$2 tmp_dir auth_a auth_b status session_id run_id cancel_run_id sse_code
  [[ $base_url =~ ^https://[A-Za-z0-9.-]+$ || $base_url =~ ^http://(127\.0\.0\.1|localhost):[0-9]+$ ]] || fail "BASE_URL must be HTTPS or loopback HTTP"
  [[ $smoke_id =~ ^[a-z0-9][a-z0-9-]{2,31}$ ]] || fail "SMOKE_ID must be a short lowercase opaque label"
  [[ -n ${SMOKE_API_KEY_A:-} && -n ${SMOKE_API_KEY_B:-} ]] || fail "SMOKE_API_KEY_A and SMOKE_API_KEY_B are required"
  [[ $SMOKE_API_KEY_A != *[$'\r\n"']* && $SMOKE_API_KEY_B != *[$'\r\n"']* ]] || fail "smoke API keys contain unsupported characters"
  command -v curl >/dev/null 2>&1 || fail "curl is required"
  command -v jq >/dev/null 2>&1 || fail "jq is required"

  umask 077
  tmp_dir=$(mktemp -d -t api-smoke.XXXXXX)
  trap "rm -rf -- '$tmp_dir'" EXIT
  auth_a="$tmp_dir/auth-a.curl"
  auth_b="$tmp_dir/auth-b.curl"
  printf 'header = "Authorization: Bearer %s"\n' "$SMOKE_API_KEY_A" > "$auth_a"
  printf 'header = "Authorization: Bearer %s"\n' "$SMOKE_API_KEY_B" > "$auth_b"

  status=$(request_status "$tmp_dir/live.json" "$base_url/health/live")
  expect_status 200 "$status" "liveness"
  status=$(request_status "$tmp_dir/ready.json" "$base_url/health/ready")
  expect_status 200 "$status" "readiness"
  status=$(request_status "$tmp_dir/anonymous.json" "$base_url/v1/sessions")
  expect_status 401 "$status" "anonymous authentication check"

  status=$(request_status "$tmp_dir/session.json" \
    --request POST \
    --config "$auth_a" \
    --header 'Content-Type: application/json' \
    --data "{\"label\":\"smoke-$smoke_id\"}" \
    "$base_url/v1/sessions")
  expect_status 201 "$status" "session creation"
  session_id=$(jq -er '.session_id | strings' "$tmp_dir/session.json")

  status=$(request_status "$tmp_dir/foreign.json" --config "$auth_b" "$base_url/v1/sessions/$session_id")
  expect_status 404 "$status" "cross-tenant isolation"

  status=$(request_status "$tmp_dir/run.json" \
    --request POST \
    --config "$auth_a" \
    --header 'Content-Type: application/json' \
    --header "Idempotency-Key: $smoke_id-stream" \
    --data "{\"query\":\"bounded cloud smoke $smoke_id\"}" \
    "$base_url/v1/sessions/$session_id/runs")
  expect_status 202 "$status" "run submission"
  run_id=$(jq -er '.run_id | strings' "$tmp_dir/run.json")

  sse_code=0
  curl --silent --show-error --no-buffer --max-time 30 \
    --config "$auth_a" \
    --output "$tmp_dir/events.txt" \
    "$base_url/v1/runs/$run_id/events" || sse_code=$?
  [[ $sse_code -eq 0 || $sse_code -eq 28 ]] || fail "SSE request failed with curl exit $sse_code"
  grep -Eq '^event: run\.' "$tmp_dir/events.txt" || fail "SSE stream contained no typed run event"

  status=$(request_status "$tmp_dir/cancel-run.json" \
    --request POST \
    --config "$auth_a" \
    --header 'Content-Type: application/json' \
    --header "Idempotency-Key: $smoke_id-cancel" \
    --data "{\"query\":\"cancel cloud smoke $smoke_id\"}" \
    "$base_url/v1/sessions/$session_id/runs")
  expect_status 202 "$status" "cancellation run submission"
  cancel_run_id=$(jq -er '.run_id | strings' "$tmp_dir/cancel-run.json")
  status=$(request_status "$tmp_dir/cancel.json" --request POST --config "$auth_a" "$base_url/v1/runs/$cancel_run_id/cancel")
  expect_status 202 "$status" "run cancellation"
  jq -e '.cancellation_requested == true' "$tmp_dir/cancel.json" >/dev/null || fail "run did not record a cancellation request"

  status=$(request_status "$tmp_dir/delete.json" --request DELETE --config "$auth_a" "$base_url/v1/sessions/$session_id")
  expect_status 204 "$status" "session deletion"
  status=$(request_status "$tmp_dir/deleted.json" --config "$auth_a" "$base_url/v1/sessions/$session_id")
  expect_status 404 "$status" "Firestore-backed deletion verification"
  printf 'API smoke passed: health, auth, run, SSE, cancel, tenant isolation, and deletion\n'
}

main "$@"
