UV ?= uv

.PHONY: sync lock-check format-check lint type test local-acceptance local-submission audit-submission check

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

local-acceptance:
	UV_BIN="$(UV)" ./task-03-deployment-strategy/scripts/local_acceptance.sh

local-submission:
	UV_BIN="$(UV)" ./scripts/local_submission_check.sh

audit-submission:
	$(UV) run python scripts/audit_submission.py

check: lock-check format-check lint type test audit-submission
