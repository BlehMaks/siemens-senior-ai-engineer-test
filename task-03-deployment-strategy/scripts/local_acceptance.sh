#!/usr/bin/env bash
set -euo pipefail

LOCAL_ACCEPTANCE_TEMP_DIR=""
LOCAL_ACCEPTANCE_SERVER_PID=""

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

cleanup() {
  if [[ -n ${LOCAL_ACCEPTANCE_SERVER_PID:-} ]]; then
    kill "$LOCAL_ACCEPTANCE_SERVER_PID" >/dev/null 2>&1 || true
    wait "$LOCAL_ACCEPTANCE_SERVER_PID" >/dev/null 2>&1 || true
  fi
  [[ -z ${LOCAL_ACCEPTANCE_TEMP_DIR:-} ]] ||
    rm -rf -- "$LOCAL_ACCEPTANCE_TEMP_DIR"
}

main() {
  [[ $# -eq 0 ]] || fail "usage: local_acceptance.sh"
  for tool in curl jq openssl uv; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
  done

  repository_root=$(git rev-parse --show-toplevel)
  cd "$repository_root"
  LOCAL_ACCEPTANCE_TEMP_DIR=$(mktemp -d -t sai-local-acceptance.XXXXXX)
  trap cleanup EXIT
  export UV_PROJECT_ENVIRONMENT="$LOCAL_ACCEPTANCE_TEMP_DIR/venv"
  uv sync --locked --all-packages --dev

  database_path="$LOCAL_ACCEPTANCE_TEMP_DIR/agent-api.sqlite3"
  server_log="$LOCAL_ACCEPTANCE_TEMP_DIR/server.log"
  port=${LOCAL_ACCEPTANCE_PORT:-8091}
  [[ $port =~ ^[0-9]{4,5}$ ]] || fail "LOCAL_ACCEPTANCE_PORT must be a four or five digit port"
  pepper=$(openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n')

  api_key_a=$(
    AGENT_API_KEY_PEPPER=$pepper uv run --frozen --all-packages \
      agent-api-key-admin --db "$database_path" create \
      --tenant-id local-smoke-a \
      --scope sessions:read --scope sessions:write \
      --scope runs:read --scope runs:write
  )
  api_key_b=$(
    AGENT_API_KEY_PEPPER=$pepper uv run --frozen --all-packages \
      agent-api-key-admin --db "$database_path" create \
      --tenant-id local-smoke-b \
      --scope sessions:read --scope sessions:write \
      --scope runs:read --scope runs:write
  )

  AGENT_API_DATABASE_PATH=$database_path \
  AGENT_API_KEY_PEPPER=$pepper \
  AGENT_API_INFERENCE_MODE=fake \
  PORT=$port \
    uv run --frozen --all-packages python -m deployment_strategy.container \
    >"$server_log" 2>&1 &
  LOCAL_ACCEPTANCE_SERVER_PID=$!

  ready=false
  for _ in $(seq 1 60); do
    if curl --silent --fail --max-time 1 \
      "http://127.0.0.1:$port/health/ready" >/dev/null; then
      ready=true
      break
    fi
    sleep 0.5
  done
  if [[ $ready != true ]]; then
    tail -n 80 "$server_log" >&2
    fail "local API did not become ready"
  fi

  SMOKE_API_KEY_A=$api_key_a \
  SMOKE_API_KEY_B=$api_key_b \
    "$repository_root/task-03-deployment-strategy/scripts/api_smoke.sh" \
    "http://127.0.0.1:$port" local-agent
  printf 'local acceptance passed without an external LLM engine\n'
}

main "$@"
