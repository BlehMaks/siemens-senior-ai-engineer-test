#!/usr/bin/env bash
set -euo pipefail

LIVE_SMOKE_TEMP_DIR=""

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

cleanup() {
  [[ -z ${LIVE_SMOKE_TEMP_DIR:-} ]] || rm -rf -- "$LIVE_SMOKE_TEMP_DIR"
}

request_status() {
  local output=$1
  shift
  curl --silent --show-error --connect-timeout 5 --max-time 30 \
    --output "$output" --write-out '%{http_code}' "$@"
}

expect_status() {
  local expected=$1 actual=$2 operation=$3
  [[ $actual == "$expected" ]] ||
    fail "$operation returned HTTP $actual, expected $expected"
}

validate_public_citations() {
  local response_path=$1 python_bin=${PYTHON_BIN:-python3}
  "$python_bin" - "$response_path" <<'PY'
import ipaddress
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit


def reject(reason: str) -> None:
    print(f"public-web citation validation failed: {reason}", file=sys.stderr)
    raise SystemExit(1)


try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    citations = payload["answer"]["citations"]
    if type(citations) is not list or not citations:
        reject("citations are missing")
    for citation in citations:
        if type(citation) is not dict or type(citation.get("source_url")) is not str:
            reject("citation URL is missing")
        parsed = urlsplit(citation["source_url"])
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            reject("citation URL is not a clean HTTP origin")
        if parsed.port not in {None, 80, 443}:
            reject("citation URL uses a non-public port")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (
                "." not in host
                or host == "localhost"
                or host.endswith((".localhost", ".local", ".internal", ".home.arpa"))
            ):
                reject("citation hostname is not public")
        else:
            if not address.is_global:
                reject("citation IP address is not public")
except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
    reject("response is invalid")
PY
}

main() {
  [[ $# -eq 2 ]] || fail "usage: live_api_smoke.sh BASE_URL OUTPUT_DIR"
  local base_url=${1%/} output_dir=$2 timeout query smoke_id auth status
  local session_id run_id state deadline

  [[ $base_url =~ ^http://(127\.0\.0\.1|localhost):[1-9][0-9]{3,4}$ ]] ||
    fail "BASE_URL must be loopback HTTP"
  [[ -n ${LIVE_API_KEY:-} ]] || fail "LIVE_API_KEY is required"
  [[ $LIVE_API_KEY != *[$'\r\n"']* ]] || fail "LIVE_API_KEY contains unsupported characters"
  command -v curl >/dev/null 2>&1 || fail "curl is required"
  command -v jq >/dev/null 2>&1 || fail "jq is required"
  command -v openssl >/dev/null 2>&1 || fail "openssl is required"
  command -v "${PYTHON_BIN:-python3}" >/dev/null 2>&1 || fail "python3 is required"

  timeout=${LIVE_RUN_TIMEOUT_SECONDS:-600}
  [[ $timeout =~ ^[1-9][0-9]*$ && $timeout -ge 60 && $timeout -le 1800 ]] ||
    fail "LIVE_RUN_TIMEOUT_SECONDS must be between 60 and 1800"
  query=${LIVE_RESEARCH_QUERY:-"Using current public web sources, summarize two sustainability commitments Siemens states on its official website. Cite each claim."}
  [[ ${#query} -ge 3 && ${#query} -le 400 ]] ||
    fail "LIVE_RESEARCH_QUERY must contain 3 to 400 characters"

  mkdir -p "$output_dir"
  LIVE_SMOKE_TEMP_DIR=$(mktemp -d -t sai-live-smoke.XXXXXX)
  trap cleanup EXIT
  auth="$LIVE_SMOKE_TEMP_DIR/auth.curl"
  umask 077
  printf 'header = "Authorization: Bearer %s"\n' "$LIVE_API_KEY" > "$auth"
  smoke_id="live-$(openssl rand -hex 6)"

  status=$(request_status "$LIVE_SMOKE_TEMP_DIR/ready.json" "$base_url/health/ready")
  expect_status 200 "$status" "readiness"

  jq -n --arg label "$smoke_id" '{label: $label}' > "$LIVE_SMOKE_TEMP_DIR/session-request.json"
  status=$(request_status "$LIVE_SMOKE_TEMP_DIR/session.json" \
    --request POST \
    --config "$auth" \
    --header 'Content-Type: application/json' \
    --data-binary "@$LIVE_SMOKE_TEMP_DIR/session-request.json" \
    "$base_url/v1/sessions")
  expect_status 201 "$status" "session creation"
  session_id=$(jq -er '.session_id | strings' "$LIVE_SMOKE_TEMP_DIR/session.json")

  jq -n --arg query "$query" '{query: $query}' > "$LIVE_SMOKE_TEMP_DIR/run-request.json"
  status=$(request_status "$LIVE_SMOKE_TEMP_DIR/run.json" \
    --request POST \
    --config "$auth" \
    --header 'Content-Type: application/json' \
    --header "Idempotency-Key: $smoke_id" \
    --data-binary "@$LIVE_SMOKE_TEMP_DIR/run-request.json" \
    "$base_url/v1/sessions/$session_id/runs")
  expect_status 202 "$status" "live run submission"
  run_id=$(jq -er '.run_id | strings' "$LIVE_SMOKE_TEMP_DIR/run.json")

  printf 'Live research run %s submitted; waiting up to %ss...\n' "$run_id" "$timeout"
  deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    status=$(request_status "$output_dir/result.json" \
      --config "$auth" "$base_url/v1/runs/$run_id")
    expect_status 200 "$status" "run status"
    state=$(jq -er '.state | strings' "$output_dir/result.json")
    case "$state" in
      completed) break ;;
      failed|expired|cancelled)
        jq '.failure // {code: "cancelled"}' "$output_dir/result.json" >&2
        fail "live research run ended in state $state"
        ;;
      queued|running|waiting_for_tool) sleep 2 ;;
      *) fail "live research run returned unknown state $state" ;;
    esac
  done
  [[ ${state:-} == completed ]] || fail "live research run timed out"

  jq -e '
    .state == "completed" and
    (.answer.answer_text | type == "string" and length >= 20) and
    (.answer.citations | type == "array" and length >= 1) and
    all(.answer.citations[];
      (.claim | type == "string" and length >= 1) and
      (.evidence_id | type == "string" and length >= 1) and
      (.source_url | type == "string" and test("^https?://"))
    )
  ' "$output_dir/result.json" >/dev/null ||
    fail "completed live run did not contain a grounded answer with citations"
  validate_public_citations "$output_dir/result.json" ||
    fail "completed live run did not contain public-web citations"

  jq -n \
    --arg session_id "$session_id" \
    --arg run_id "$run_id" \
    --arg query "$query" \
    --arg model "${LIVE_MODEL_NAME:-unknown}" \
    --argjson citation_count "$(jq '.answer.citations | length' "$output_dir/result.json")" \
    '{
      mode: "ollama-live",
      model: $model,
      query: $query,
      session_id: $session_id,
      run_id: $run_id,
      state: "completed",
      citation_count: $citation_count
    }' > "$output_dir/summary.json"

  printf '\nLive grounded answer:\n'
  jq -r '.answer.answer_text' "$output_dir/result.json"
  printf '\nCitations:\n'
  jq -r '.answer.citations[] | "- \(.source_url) — \(.claim)"' "$output_dir/result.json"
  printf '\nLive API smoke passed: Ollama-backed run completed with public-web citations.\n'
}

main "$@"
