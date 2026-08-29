UV ?= uv

.PHONY: sync lock-check format-check lint type test local-acceptance audit-submission check

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
	./task-03-deployment-strategy/scripts/local_acceptance.sh

audit-submission:
	$(UV) run python scripts/audit_submission.py

check: lock-check format-check lint type test audit-submission
