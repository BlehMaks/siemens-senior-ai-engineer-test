#!/usr/bin/env bash
set -euo pipefail

LIVE_ACCEPTANCE_TEMP_DIR=""
LIVE_ACCEPTANCE_API_PID=""
LIVE_ACCEPTANCE_OLLAMA_PID=""

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

cleanup() {
  if [[ -n ${LIVE_ACCEPTANCE_API_PID:-} ]]; then
    kill "$LIVE_ACCEPTANCE_API_PID" >/dev/null 2>&1 || true
    wait "$LIVE_ACCEPTANCE_API_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n ${LIVE_ACCEPTANCE_OLLAMA_PID:-} ]]; then
    kill "$LIVE_ACCEPTANCE_OLLAMA_PID" >/dev/null 2>&1 || true
    wait "$LIVE_ACCEPTANCE_OLLAMA_PID" >/dev/null 2>&1 || true
  fi
  [[ -z ${LIVE_ACCEPTANCE_TEMP_DIR:-} ]] ||
    rm -rf -- "$LIVE_ACCEPTANCE_TEMP_DIR"
}

usage() {
  cat <<'EOF'
Usage: local_live_acceptance.sh [options]

Options:
  --setup install|existing  Install Ollama/model or use an existing installation.
  --model NAME              Ollama model tag (default: qwen3:8b).
  --base-url URL            Loopback Ollama URL (default: http://127.0.0.1:11434).
  --port PORT               Local API port (default: 8093).
  --query TEXT              Live research query (3-400 characters).
  --skip-deterministic      Skip the normal repository gate if it already passed.
  --keep-running            Keep API and script-started Ollama alive until Ctrl+C.
  -h, --help                Show this help.

Without --setup, the script starts with an interactive setup choice.
EOF
}

install_ollama() {
  local system installer
  command -v ollama >/dev/null 2>&1 && return
  system=$(uname -s)
  case "$system" in
    Darwin)
      command -v brew >/dev/null 2>&1 ||
        fail "Homebrew is required for automatic macOS setup; install Ollama from https://ollama.com/download/mac and rerun with --setup existing"
      printf 'Installing Ollama with Homebrew...\n'
      brew install ollama
      ;;
    Linux)
      installer="$LIVE_ACCEPTANCE_TEMP_DIR/ollama-install.sh"
      printf 'Downloading the official Ollama installer for review and execution...\n'
      curl --fail --show-error --location \
        https://ollama.com/install.sh --output "$installer"
      sh "$installer"
      ;;
    *)
      fail "automatic Ollama installation supports macOS and Linux/WSL2 only"
      ;;
  esac
  command -v ollama >/dev/null 2>&1 || fail "Ollama installation did not provide the ollama command"
}

ollama_ready() {
  curl --silent --fail --connect-timeout 2 --max-time 3 \
    "$model_base_url/api/tags" >/dev/null 2>&1
}

start_ollama() {
  local ollama_host=${model_base_url#http://}
  ollama_ready && return
  printf 'Starting Ollama on %s...\n' "$model_base_url"
  OLLAMA_HOST="$ollama_host" ollama serve > "$run_dir/ollama.log" 2>&1 &
  LIVE_ACCEPTANCE_OLLAMA_PID=$!
  for _ in $(seq 1 60); do
    ollama_ready && return
    sleep 1
  done
  tail -n 80 "$run_dir/ollama.log" >&2 || true
  fail "Ollama did not become ready within 60 seconds"
}

prepare_model() {
  local ollama_host=${model_base_url#http://}
  if OLLAMA_HOST="$ollama_host" ollama show "$model_name" >/dev/null 2>&1; then
    printf 'Using installed Ollama model %s.\n' "$model_name"
    return
  fi
  [[ $setup_mode == install ]] ||
    fail "model $model_name is not installed; rerun with --setup install or run: ollama pull $model_name"
  printf 'Downloading Ollama model %s. This may take several minutes...\n' "$model_name"
  OLLAMA_HOST="$ollama_host" ollama pull "$model_name"
  OLLAMA_HOST="$ollama_host" ollama show "$model_name" >/dev/null 2>&1 ||
    fail "Ollama did not expose model $model_name after pull"
}

select_interactive_setup() {
  local choice selected_model
  printf '\nLocal live-model preparation\n'
  printf '  1) Install/start Ollama and download a model if needed\n'
  printf '  2) Ollama and the model are already installed; configure and test only\n'
  printf '  3) Exit without changes\n'
  read -r -p 'Choose [1-3]: ' choice
  case "$choice" in
    1) setup_mode=install ;;
    2) setup_mode=existing ;;
    3) printf 'Cancelled.\n'; exit 0 ;;
    *) fail "choose 1, 2, or 3" ;;
  esac
  if [[ -z $model_name ]]; then
    read -r -p 'Ollama model tag [qwen3:8b]: ' selected_model
    model_name=${selected_model:-qwen3:8b}
  fi
}

main() {
  local setup_mode="" model_name=${LIVE_MODEL_NAME:-}
  local model_base_url=${AGENT_MODEL_BASE_URL:-http://127.0.0.1:11434}
  local api_port=${LOCAL_LIVE_ACCEPTANCE_PORT:-8093}
  local query=${LIVE_RESEARCH_QUERY:-}
  local skip_deterministic=false keep_running=false repository_root uv_bin
  local run_stamp run_dir database_path server_log pepper api_key ready response

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --setup) [[ $# -ge 2 ]] || fail "--setup requires a value"; setup_mode=$2; shift 2 ;;
      --model) [[ $# -ge 2 ]] || fail "--model requires a value"; model_name=$2; shift 2 ;;
      --base-url) [[ $# -ge 2 ]] || fail "--base-url requires a value"; model_base_url=$2; shift 2 ;;
      --port) [[ $# -ge 2 ]] || fail "--port requires a value"; api_port=$2; shift 2 ;;
      --query) [[ $# -ge 2 ]] || fail "--query requires a value"; query=$2; shift 2 ;;
      --skip-deterministic) skip_deterministic=true; shift ;;
      --keep-running) keep_running=true; shift ;;
      -h|--help) usage; exit 0 ;;
      *) fail "unknown option: $1" ;;
    esac
  done

  if [[ -z $setup_mode ]]; then
    [[ -t 0 ]] || fail "non-interactive use requires --setup install or --setup existing"
    select_interactive_setup
  fi
  [[ $setup_mode == install || $setup_mode == existing ]] ||
    fail "--setup must be install or existing"
  model_name=${model_name:-qwen3:8b}
  [[ $model_name =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$ ]] ||
    fail "model name has an invalid format"
  [[ $model_base_url =~ ^http://(127\.0\.0\.1|localhost):[1-9][0-9]{3,4}$ ]] ||
    fail "--base-url must be loopback HTTP with an explicit port"
  [[ $api_port =~ ^[1-9][0-9]{3,4}$ && $api_port -le 65535 ]] ||
    fail "--port must be a four or five digit TCP port"
  [[ -z $query || (${#query} -ge 3 && ${#query} -le 400) ]] ||
    fail "--query must contain 3 to 400 characters"

  for tool in curl jq openssl git; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
  done
  uv_bin=${UV_BIN:-uv}
  command -v "$uv_bin" >/dev/null 2>&1 || fail "$uv_bin is required"
  repository_root=$(git rev-parse --show-toplevel)
  cd "$repository_root"

  LIVE_ACCEPTANCE_TEMP_DIR=$(mktemp -d -t sai-live-acceptance.XXXXXX)
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  run_stamp="$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 3)"
  run_dir="$repository_root/artifacts/local/live-acceptance/$run_stamp"
  mkdir -p "$run_dir"

  if [[ $setup_mode == install ]]; then
    install_ollama
  else
    command -v ollama >/dev/null 2>&1 ||
      fail "Ollama is not installed; rerun with --setup install"
  fi
  start_ollama
  prepare_model

  ollama --version | tee "$run_dir/ollama-version.txt"
  OLLAMA_HOST="${model_base_url#http://}" ollama list > "$run_dir/ollama-models.txt"

  if [[ $skip_deterministic == false ]]; then
    printf '\nRunning the complete deterministic repository gate first...\n'
    UV_BIN="$uv_bin" "$repository_root/scripts/local_submission_check.sh"
  else
    printf '\nSkipping the deterministic gate as requested.\n'
  fi

  export UV_PROJECT_ENVIRONMENT="$LIVE_ACCEPTANCE_TEMP_DIR/venv"
  "$uv_bin" sync --locked --all-packages --dev
  database_path="$run_dir/agent-api.sqlite3"
  server_log="$run_dir/server.log"
  set +x
  pepper=$(openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n')
  api_key=$(
    AGENT_API_KEY_PEPPER=$pepper "$uv_bin" run --frozen --all-packages \
      agent-api-key-admin --db "$database_path" create \
      --tenant-id local-live-review \
      --scope sessions:read --scope sessions:write \
      --scope runs:read --scope runs:write
  )

  env \
    -u AGENT_API_SERVICE_ROLE \
    -u AGENT_API_GCP_PROJECT_ID \
    -u AGENT_API_FIRESTORE_DATABASE \
    -u AGENT_API_CLOUD_TASKS_QUEUE \
    -u AGENT_API_TASK_TARGET_URL \
    -u AGENT_API_TASK_SIGNING_HMAC \
    -u AGENT_API_QUEUE_DELIVERY_PATH \
    -u AGENT_MODEL_GOOGLE_ID_TOKEN_AUDIENCE \
    -u GOOGLE_APPLICATION_CREDENTIALS \
    -u GOOGLE_CLOUD_PROJECT \
    -u GCLOUD_PROJECT \
    -u GCP_PROJECT \
    -u K_SERVICE \
    AGENT_API_DATABASE_PATH="$database_path" \
    AGENT_API_KEY_PEPPER="$pepper" \
    AGENT_API_INFERENCE_MODE=ollama \
    AGENT_MODEL_NAME="$model_name" \
    AGENT_MODEL_BASE_URL="$model_base_url" \
    AGENT_MODEL_TRANSPORT_PROFILE=local \
    AGENT_SEARCH_BACKENDS=duckduckgo \
    PORT="$api_port" \
    "$uv_bin" run --frozen --all-packages python -m deployment_strategy.container \
    > "$server_log" 2>&1 &
  LIVE_ACCEPTANCE_API_PID=$!

  ready=false
  for _ in $(seq 1 90); do
    if ! kill -0 "$LIVE_ACCEPTANCE_API_PID" >/dev/null 2>&1; then
      tail -n 100 "$server_log" >&2 || true
      fail "live API process stopped before becoming ready"
    fi
    if curl --silent --fail --max-time 1 \
      "http://127.0.0.1:$api_port/health/ready" >/dev/null; then
      ready=true
      break
    fi
    sleep 1
  done
  if [[ $ready != true ]]; then
    tail -n 100 "$server_log" >&2 || true
    fail "live API did not become ready"
  fi

  printf '\nRunning a real Ollama + public-web request through the API...\n'
  LIVE_API_KEY="$api_key" \
  LIVE_MODEL_NAME="$model_name" \
  LIVE_RESEARCH_QUERY="$query" \
    "$repository_root/task-03-deployment-strategy/scripts/live_api_smoke.sh" \
    "http://127.0.0.1:$api_port" "$run_dir"

  curl --silent --fail --show-error --max-time 10 \
    "$model_base_url/api/ps" > "$run_dir/ollama-running-models.json"
  jq -e --arg model "$model_name" \
    'any(.models[]?; .name == $model or .model == $model)' \
    "$run_dir/ollama-running-models.json" >/dev/null ||
    fail "Ollama did not report $model_name as a loaded running model"

  printf '\nFull live acceptance passed. Artifacts: %s\n' "$run_dir"
  if [[ $keep_running == false && -t 0 ]]; then
    read -r -p 'Keep the live API running for manual review? [y/N]: ' response
    [[ $response =~ ^[Yy]$ ]] && keep_running=true
  fi
  if [[ $keep_running == true ]]; then
    printf 'Reviewer UI: http://127.0.0.1:%s/\n' "$api_port"
    printf 'Local API key (shown once): %s\n' "$api_key"
    printf 'Press Ctrl+C to stop the API and any Ollama server started by this script.\n'
    wait "$LIVE_ACCEPTANCE_API_PID"
  fi
}

main "$@"
