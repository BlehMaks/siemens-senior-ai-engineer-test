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

siemens_live_headline() {
  local response_path=$1 python_bin=${PYTHON_BIN:-python3}
  curl --fail --silent --show-error --location --connect-timeout 5 --max-time 20 \
    --max-filesize 5242880 \
    --header 'User-Agent: SiemensResearchAgentLiveCheck/0.1' \
    --header 'Accept: text/html' \
    --output "$response_path" \
    'https://press.siemens.com/global/en'
  "$python_bin" - "$response_path" <<'PY'
from html.parser import HTMLParser
import json
from pathlib import Path
import sys


class SiemensHeadlineParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.ignored_depth = 0
        self.title_parts: list[str] = []
        self.pending_year: int | None = None
        self.target_title: str | None = None
        self.target_year: int | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        classes = set((attributes.get("class") or "").split())
        raw_date = attributes.get("data-original") or ""
        if (
            not self.ignored_depth
            and tag == "span"
            and classes.intersection({"Date", "StartDate"})
            and len(raw_date) >= 4
            and raw_date[:4].isdigit()
        ):
            self.pending_year = int(raw_date[:4])
        if (
            not self.ignored_depth
            and tag == "h3"
            and self.target_title is None
            and self.pending_year is not None
        ):
            self.in_title = True
            self.title_parts = []

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth and self.in_title:
            self.title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if not self.ignored_depth and tag == "h3" and self.in_title:
            title = " ".join(" ".join(self.title_parts).split())
            if 1 <= len(title) <= 250:
                self.target_title = title
                self.target_year = self.pending_year
            self.in_title = False
            self.pending_year = None


parser = SiemensHeadlineParser()
try:
    parser.feed(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, UnicodeError) as exc:
    raise SystemExit("could not read Siemens press page") from exc
if parser.target_title is None or parser.target_year is None:
    raise SystemExit("Siemens press page did not contain a dated press item")
print(
    json.dumps(
        {"title": parser.target_title, "year": parser.target_year},
        ensure_ascii=True,
    )
)
PY
}

validate_public_citations() {
  local response_path=$1 python_bin=${PYTHON_BIN:-python3}
  "$python_bin" - "$response_path" <<'PY'
import json
from pathlib import Path
import sys

from agent_api.schemas import require_public_source_url


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
        require_public_source_url(citation["source_url"])
except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
    reject("response is invalid")
PY
}

validate_expected_answer() {
  local response_path=$1 expected=$2 python_bin=${PYTHON_BIN:-python3}
  "$python_bin" - "$response_path" "$expected" <<'PY'
import json
from pathlib import Path
import sys
import unicodedata


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(
        value.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})).split()
    )


try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    answer = payload["answer"]["answer_text"]
    expected = sys.argv[2]
    if type(answer) is not str or normalized(expected) not in normalized(answer):
        raise ValueError
except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
    print(
        "completed live run did not contain the independently observed live fact",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
}

main() {
  [[ $# -eq 2 ]] || fail "usage: live_api_smoke.sh BASE_URL OUTPUT_DIR"
  local base_url=${1%/} output_dir=$2 timeout query expected_answer smoke_id auth status
  local session_id run_id state deadline oracle target_year

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

  mkdir -p "$output_dir"
  LIVE_SMOKE_TEMP_DIR=$(mktemp -d -t sai-live-smoke.XXXXXX)
  trap cleanup EXIT

  query=${LIVE_RESEARCH_QUERY:-}
  expected_answer=${LIVE_EXPECTED_ANSWER_TEXT:-}
  if [[ -z $query ]]; then
    oracle=$(
      siemens_live_headline "$LIVE_SMOKE_TEMP_DIR/siemens-press.html"
    ) || fail "Siemens press-page preflight failed"
    target_year=$(jq -er '.year | numbers' <<<"$oracle") ||
      fail "Siemens press-page preflight returned an invalid year"
    query="Find and return the exact first listed headline at https://press.siemens.com/global/en dated $target_year"
    if [[ -z $expected_answer ]]; then
      expected_answer=$(jq -er '.title | strings' <<<"$oracle") ||
        fail "Siemens press-page preflight returned an invalid title"
    fi
  fi
  [[ ${#query} -ge 3 && ${#query} -le 400 ]] ||
    fail "LIVE_RESEARCH_QUERY must contain 3 to 400 characters"
  [[ -z $expected_answer || (${#expected_answer} -ge 1 && ${#expected_answer} -le 200) ]] ||
    fail "LIVE_EXPECTED_ANSWER_TEXT must contain 1 to 200 characters"

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
  if [[ -n $expected_answer ]]; then
    validate_expected_answer "$output_dir/result.json" "$expected_answer" || exit 1
  fi

  jq -n \
    --arg session_id "$session_id" \
    --arg run_id "$run_id" \
    --arg query "$query" \
    --arg expected_answer "$expected_answer" \
    --arg model "${LIVE_MODEL_NAME:-unknown}" \
    --argjson citation_count "$(jq '.answer.citations | length' "$output_dir/result.json")" \
    '{
      mode: "ollama-live",
      model: $model,
      query: $query,
      expected_answer: (if $expected_answer == "" then null else $expected_answer end),
      session_id: $session_id,
      run_id: $run_id,
      state: "completed",
      citation_count: $citation_count
    }' > "$output_dir/summary.json"

  printf '\nLive grounded answer:\n'
  jq -r '.answer.answer_text' "$output_dir/result.json"
  [[ -z $expected_answer ]] || printf '\nExpected live fact: %s\n' "$expected_answer"
  printf '\nCitations:\n'
  jq -r '.answer.citations[] | "- \(.source_url) — \(.claim)"' "$output_dir/result.json"
  printf '\nLive API smoke passed: Ollama-backed run completed with public-web citations.\n'
}

main "$@"
