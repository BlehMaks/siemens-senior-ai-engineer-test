UV ?= uv

.PHONY: sync lock-check format-check lint type test check

sync:
	$(UV) sync --locked

lock-check:
	$(UV) lock --check

format-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .

type:
	$(UV) run mypy task-*/src

test:
	$(UV) run pytest

check: lock-check format-check lint type test

