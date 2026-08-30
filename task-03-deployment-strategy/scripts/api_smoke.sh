#!/usr/bin/env bash
set -euo pipefail

API_SMOKE_TEMP_DIR=""

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

cleanup_temp_dir() {
  # A function trap keeps the generated path quoted as data instead of reparsing it.
  [[ -z ${API_SMOKE_TEMP_DIR:-} ]] || rm -rf -- "$API_SMOKE_TEMP_DIR"
}

expect_status() {
  local expected=$1 actual=$2 operation=$3
  [[ $actual == "$expected" ]] || fail "$operation returned HTTP $actual, expected $expected"
}

request_status() {
  local output=$1
  shift
  curl --silent --show-error --connect-timeout 5 --max-time 15 \
    --output "$output" --write-out '%{http_code}' "$@"
}

validate_cancellation_response() {
  local response_path=$1 expected_run_id=$2 python_bin=${PYTHON_BIN:-python3}
  "$python_bin" - "$response_path" "$expected_run_id" <<'PY'
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys


def reject() -> None:
    raise SystemExit(1)


try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if type(payload) is not dict:
        reject()
    if set(payload) != {
        "run_id",
        "state",
        "cancellation_requested",
        "changed",
        "requested_at",
    }:
        reject()
    if payload["run_id"] != sys.argv[2] or type(payload["run_id"]) is not str:
        reject()
    if type(payload["state"]) is not str:
        reject()
    if type(payload["cancellation_requested"]) is not bool:
        reject()
    if type(payload["changed"]) is not bool:
        reject()

    requested_at = payload["requested_at"]
    if requested_at is not None:
        if type(requested_at) is not str:
            reject()
        normalized = requested_at[:-1] + "+00:00" if requested_at.endswith("Z") else requested_at
        parsed = datetime.fromisoformat(normalized)
        if parsed.utcoffset() != timedelta(0):
            reject()

    active_cancellation = (
        payload["cancellation_requested"] is True
        and payload["changed"] is True
        and requested_at is not None
        and payload["state"] in {"cancelled", "running", "waiting_for_tool"}
    )
    terminal_race = (
        payload["cancellation_requested"] is False
        and payload["changed"] is False
        and requested_at is None
        and payload["state"] in {"completed", "failed", "expired"}
    )
    if not (active_cancellation or terminal_race):
        reject()
except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
    reject()
PY
}

main() {
  [[ $# -eq 2 ]] || fail "usage: api_smoke.sh BASE_URL SMOKE_ID"
  local base_url=${1%/} smoke_id=$2 tmp_dir auth_a auth_b status session_id run_id cancel_run_id sse_code sse_status
  [[ $base_url =~ ^https://[A-Za-z0-9.-]+$ || $base_url =~ ^http://(127\.0\.0\.1|localhost):[0-9]+$ ]] || fail "BASE_URL must be HTTPS or loopback HTTP"
  [[ $smoke_id =~ ^[a-z0-9][a-z0-9-]{2,31}$ ]] || fail "SMOKE_ID must be a short lowercase opaque label"
  [[ -n ${SMOKE_API_KEY_A:-} && -n ${SMOKE_API_KEY_B:-} ]] || fail "SMOKE_API_KEY_A and SMOKE_API_KEY_B are required"
  [[ $SMOKE_API_KEY_A != *[$'\r\n"']* && $SMOKE_API_KEY_B != *[$'\r\n"']* ]] || fail "smoke API keys contain unsupported characters"
  command -v curl >/dev/null 2>&1 || fail "curl is required"
  command -v jq >/dev/null 2>&1 || fail "jq is required"
  command -v "${PYTHON_BIN:-python3}" >/dev/null 2>&1 || fail "python3 is required"

  umask 077
  tmp_dir=$(mktemp -d -t api-smoke.XXXXXX)
  API_SMOKE_TEMP_DIR=$tmp_dir
  trap cleanup_temp_dir EXIT
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
  sse_status=""
  curl --silent --show-error --no-buffer --max-time 30 \
    --config "$auth_a" \
    --output "$tmp_dir/events.txt" \
    --write-out '%{http_code}' \
    "$base_url/v1/runs/$run_id/events" >"$tmp_dir/events.status" || sse_code=$?
  [[ $sse_code -eq 0 || $sse_code -eq 28 ]] || fail "SSE request failed with curl exit $sse_code"
  sse_status=$(<"$tmp_dir/events.status")
  expect_status 200 "$sse_status" "SSE stream"
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
  validate_cancellation_response "$tmp_dir/cancel.json" "$cancel_run_id" ||
    fail "run cancellation response violated the active-or-terminal race contract"

  status=$(request_status "$tmp_dir/delete.json" --request DELETE --config "$auth_a" "$base_url/v1/sessions/$session_id")
  expect_status 204 "$status" "session deletion"
  status=$(request_status "$tmp_dir/deleted.json" --config "$auth_a" "$base_url/v1/sessions/$session_id")
  expect_status 404 "$status" "Firestore-backed deletion verification"
  printf 'API smoke passed: health, auth, run, SSE, cancel, tenant isolation, and deletion\n'
}

main "$@"
