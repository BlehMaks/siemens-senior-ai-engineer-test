#!/usr/bin/env bash
set -euo pipefail

LOCAL_SUBMISSION_TEMP_DIR=""

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

cleanup() {
  [[ -z ${LOCAL_SUBMISSION_TEMP_DIR:-} ]] ||
    rm -rf -- "$LOCAL_SUBMISSION_TEMP_DIR"
}

validate_optional_inputs() {
  if [[ ${SIEMENS_TASK4_INPUT_DIR+x} == x && -z $SIEMENS_TASK4_INPUT_DIR ]]; then
    unset SIEMENS_TASK4_INPUT_DIR
  fi
  if [[ ${SIEMENS_FUSE_CSV+x} == x && -z $SIEMENS_FUSE_CSV ]]; then
    unset SIEMENS_FUSE_CSV
  fi
  if [[ -n ${SIEMENS_TASK4_INPUT_DIR:-} ]]; then
    [[ -f $SIEMENS_TASK4_INPUT_DIR/Training_part1.csv ]] ||
      fail "SIEMENS_TASK4_INPUT_DIR is missing Training_part1.csv"
    [[ -f $SIEMENS_TASK4_INPUT_DIR/Training_part2.csv ]] ||
      fail "SIEMENS_TASK4_INPUT_DIR is missing Training_part2.csv"
  fi
  if [[ -n ${SIEMENS_FUSE_CSV:-} ]]; then
    [[ -f $SIEMENS_FUSE_CSV ]] || fail "SIEMENS_FUSE_CSV does not name a file"
  fi
}

main() {
  [[ $# -eq 0 ]] || fail "usage: local_submission_check.sh"
  UV_BIN=${UV_BIN:-uv}
  command -v "$UV_BIN" >/dev/null 2>&1 || fail "$UV_BIN is required"
  export UV_BIN

  repository_root=$(git rev-parse --show-toplevel)
  cd "$repository_root"
  validate_optional_inputs

  LOCAL_SUBMISSION_TEMP_DIR=$(mktemp -d -t sai-local-submission.XXXXXX)
  trap cleanup EXIT
  export UV_PROJECT_ENVIRONMENT="$LOCAL_SUBMISSION_TEMP_DIR/venv"

  "$UV_BIN" sync --locked --all-packages --all-groups
  "$UV_BIN" lock --check
  "$UV_BIN" run --frozen ruff format --check .
  "$UV_BIN" run --frozen ruff check .
  "$UV_BIN" run --frozen mypy task-*/src scripts
  "$UV_BIN" run --frozen pytest -q
  "$UV_BIN" run --frozen python scripts/audit_submission.py

  "$repository_root/task-03-deployment-strategy/scripts/local_acceptance.sh"
  printf 'local submission check passed for Tasks 1 through 6\n'
}

main "$@"
