UV ?= uv

.PHONY: sync lock-check format-check lint type test coverage-report local-acceptance local-live-acceptance local-submission web-agent audit-language audit-links audit-submission check

sync:
	$(UV) sync --locked --all-packages --dev

lock-check:
	$(UV) lock --check

format-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .

type:
	$(UV) run mypy task-*/src scripts

test:
	$(UV) run pytest

coverage-report:
	$(UV) run coverage erase
	$(UV) run coverage run -m pytest
	$(UV) run coverage report

local-acceptance:
	UV_BIN="$(UV)" ./task-03-deployment-strategy/scripts/local_acceptance.sh

local-live-acceptance:
	UV_BIN="$(UV)" ./task-03-deployment-strategy/scripts/local_live_acceptance.sh

local-submission:
	UV_BIN="$(UV)" ./scripts/local_submission_check.sh

# Task 1 baseline agent: prose answers with the sources it opened, run against a
# local Ollama model. Override MODEL_NAME or MODEL_URL to use a different model
# or any other OpenAI-compatible endpoint.
web-agent:
	@test -n "$(Q)" || { echo 'Usage: make web-agent Q="your question"'; exit 2; }
	MODEL_URL="$${MODEL_URL:-http://127.0.0.1:11434/v1}" \
	MODEL_NAME="$${MODEL_NAME:-qwen3:8b}" \
	$(UV) run python task-01-search-agent/baseline/web_agent.py "$(Q)"

audit-submission:
	$(UV) run python scripts/audit_submission.py

audit-language:
	$(UV) run python scripts/audit_language.py

audit-links:
	$(UV) run python scripts/audit_links.py

check: lock-check format-check lint type test audit-language audit-links audit-submission
