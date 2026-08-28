# Reviewed memory evaluation notes

## Decision under test

The implemented candidate is a removable, default-off read seam for manually
reviewed semantic facts and active procedure versions. It does not evaluate or enable
model-generated proposals. The M5 model benchmark remains excluded.

## Frozen before/after fixture

`evals/fixtures/reviewed-memory.json` declares the deterministic comparison:

| Mode | Reader calls | Synthesis field | Expected behavior |
| --- | ---: | --- | --- |
| Disabled (default) | 0 | absent | Existing planning, tools, evidence, answer validation, and prompt payload remain unchanged. |
| Enabled | 1 | `reviewed_memory_untrusted_data` | At most eight approved/non-expired facts and four approved active procedures appear as reduced untrusted data. |

The fixture contains public synthetic records only. Tests compare the exact reduced
payload, so field additions, origin/reviewer leakage, or nondeterministic ordering
fail the gate.

## Executable gates

- `tests/memory/test_context.py` covers active-only selection, expiry, hard caps,
  reduced serialization, malicious post-validation records, and deletion without a
  cache.
- `tests/test_runner.py` compares disabled/enabled prompt shape, proves planning and
  search still run independently, checks the memory precedence statement remains in
  the system prompt rather than stored data, and rejects forged memory before the
  provider call.
- semantic/procedural repository suites cover review transitions, conflicts,
  version races, ABA, corruption, reopen, source/session/tenant deletion, and hostile
  text/container inputs.
- Task 2 storage tests prove session deletion cascades or explicitly removes
  reflections, facts, procedure versions, and active pointers while preserving safe
  consumed-version heads; tenant deletion removes the heads.

Run from the repository root:

```bash
uv run --locked pytest -q task-01-search-agent/tests/memory \
  task-01-search-agent/tests/test_runner.py \
  task-02-agent-api/tests/storage/test_migrations.py \
  task-02-agent-api/tests/storage/test_repositories.py
uv run --locked mypy task-*/src scripts
uv run --locked ruff check .
```

## Interpretation

The deterministic result establishes lifecycle, isolation, precedence, deletion, and
bounded integration mechanics. It makes no claim that memory improves answer
quality. Promotion of the read seam beyond opt-in requires a separate frozen quality
evaluation; model-generated proposals additionally require the evidence listed in
ADR-0003 and the threat model. Until then, default-off is the honest outcome.
