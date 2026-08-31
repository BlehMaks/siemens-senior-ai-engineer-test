UV ?= uv

.PHONY: sync lock-check format-check lint type test coverage-report local-acceptance local-live-acceptance local-submission audit-language audit-links audit-submission check

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

audit-submission:
	$(UV) run python scripts/audit_submission.py

audit-language:
	$(UV) run python scripts/audit_language.py

audit-links:
	$(UV) run python scripts/audit_links.py

check: lock-check format-check lint type test audit-language audit-links audit-submission
