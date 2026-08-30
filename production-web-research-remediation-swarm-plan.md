# Production web-research remediation swarm plan

## Goal and execution model

Execute the approved production remediation in parallel without overlapping file
ownership. All workers share the same working tree, must preserve other workers'
changes, must not commit, and must report changed files plus focused test results.
The root agent integrates, runs global verification, creates atomic commits, and owns
the two requested adversarial-review/fix cycles.

## Shared contracts from the architecture plan

All workers must follow the contracts in `production-web-research-remediation-plan.md`.
The practical interface freeze for parallel work is:

- `ResearchDocument` and `ResearchChunk` are Task 1 internal data objects. They carry
  deterministic IDs, canonical URL, title, media type, source type, page/section
  provenance, content hash, timestamps, bounded text, and ranking features.
- `SelectedContext` is the only retrieval output the runner consumes: ordered chunks,
  quote strings, total character count, score components, and a context hash.
- `ConversationTurn` crosses from Task 2 into Task 1. It contains only prior completed
  public user request and public answer for the same tenant/session.
- `ActionTraceRecord` crosses from Task 1 into Task 2 persistence/logging. It contains
  bounded stage/action/outcome metadata, safe IDs, counts, durations, reason codes,
  usage deltas, and optional context hash. It never contains raw page text, prompts,
  hidden reasoning, credentials, exception bodies, tenant IDs as metric labels, or
  unbounded query/domain fields.
- Runtime profile names are `local` and `cloud`; inference mode names remain `fake`,
  `disabled`, and `ollama`.
- Search backend order is configured as a bounded comma-separated allow-list with
  `auto` as the production default.

## Assumptions and non-goals

- The implementation may add a small dependency such as `pypdf`, but every new
  external API was checked through Context7 before planning.
- Ollama-compatible cloud service is valid for rollout when exposed as a private HTTPS
  endpoint with ID-token auth and Cloud Run invoker IAM.
- OCR, browser fallback, embeddings, caches, adaptive second-pass research, SIEM/DLP,
  and enterprise load certification are documented extension points, not part of this
  implementation wave.
- User-facing API shape must stay backward compatible. New trace fields are internal
  and versioned through reflection/observability payloads.

## Dependency graph

```text
S0 acceptance contracts
  ├── Wave 1: S1 document/PDF/chunk contracts ──┐
  ├── Wave 1: S2 search fallback/config ────────┼── S5 integration
  └── Wave 1: S4 cloud dual-mode wiring ────────┘       │
                                                         │
S1 frozen interface -> Wave 2: S3 context/trace/runner ──┘

S5 -> S6 adversarial review 1 -> S7 fixes -> S8 adversarial review 2 -> S9 fixes
```

## Work packages

### S0 — acceptance contracts (root, blocking)

- Freeze public compatibility expectations and failure mapping.
- Confirm internal `ResearchDocument`, `ResearchChunk`, `SelectedContext`,
  `ConversationTurn`, `ActionTraceRecord`, `ResearchTraceSink`, and transport profile
  shapes.
- Add/update the implementation plan after plan-checker and architect feedback.
- Dependencies: none.
- Unlocks: S1, S2, S3, S4.

### S1 — document/PDF/chunk retrieval worker

- Ownership:
  - `task-01-search-agent/src/search_agent/tools/fetch.py`
  - `task-01-search-agent/src/search_agent/tools/extract.py`
  - new Task 1 document/chunk/retrieval modules
  - corresponding Task 1 tool/evidence/retrieval/e2e tests and frozen fixtures
  - Task 1 `pyproject.toml` dependency entry
- Deliverables:
  - bounded isolated PDF extraction with page provenance;
  - HTML table preservation;
  - deterministic structural chunking, exact dedup, authority/freshness/lexical rank,
    top-k selection;
  - pure selected context/quotes output for runner integration;
  - late-document and PDF/table focused retrieval fixtures.
- Interface produced:
  - A pure Task 1 retrieval function/class that accepts request text, search hit, and
    extracted document and returns bounded selected context/quotes.
  - Extraction result metadata for PDF pages and tables must be optional so existing
    HTML/plain tests continue to pass.
- Must not edit evidence/runner/runtime/CLI/Terraform files.
- Dependencies: S0.
- Focused tests: extraction, fetch, evidence, retrieval, frozen corpus.

### S2 — search/runtime worker

- Ownership:
  - `task-01-search-agent/src/search_agent/tools/search.py`
  - `task-01-search-agent/src/search_agent/runtime.py`
  - `task-01-search-agent/src/search_agent/cli.py`
  - matching search/runtime/CLI tests
- Deliverables:
  - `auto` default;
  - bounded ordered fallback with typed attempt outcomes;
  - CLI/env backend configuration;
  - local/cloud model transport-profile validation seam.
- Interface produced:
  - Runtime settings expose `search_backends` as an ordered tuple while preserving a
    compatibility path for existing single-backend tests.
  - Attempt metadata is available to S3 logging through safe bounded fields, not raw
    backend exceptions.
- Coordinate Task 1 runner constructor changes with S3; do not revert S1 imports.
- Dependencies: S0.
- Focused tests: search adapter, runtime, CLI, Ollama provider configuration.

### S3 — conversation, trace, and logging worker

- Ownership:
  - `task-01-search-agent/src/search_agent/contracts.py`
  - `task-01-search-agent/src/search_agent/planning.py`
  - `task-01-search-agent/src/search_agent/runner.py`
  - `task-01-search-agent/src/search_agent/evidence.py` runner-facing integration
  - Task 1 episodic reflection contracts/derivation
  - `task-02-agent-api/src/agent_api/ports.py`
  - `task-02-agent-api/src/agent_api/storage/repositories.py`
  - `task-02-agent-api/src/agent_api/storage/cloud.py`
  - `task-02-agent-api/src/agent_api/observability.py`
  - `task-02-agent-api/src/agent_api/workers/local.py`
  - minimal Task 2 executor-context decorator/composition files
  - matching runner/memory/repository/worker/observability tests
- Deliverables:
  - bounded same-session conversation context;
  - typed stage/action trace;
  - safe JSON action logs and persisted bounded trace;
  - expanded internal failure taxonomy with compatible public mapping.
- Interface consumed:
  - S1 selected context/quotes enter the runner before synthesis.
  - S2 search attempt metadata becomes action trace records.
- Interface produced:
  - `RunResult` carries a bounded immutable action trace.
  - Task 2 persistence stores a schema-versioned trace summary inside the existing
    reflection boundary.
- Must not edit Terraform or search/fetch/extract implementation.
- Dependencies: S1 and S2; consumes S1 chunk-selection result and S2 attempt metadata
  through narrow ports.
- Focused tests: runner, planning, reflections, worker, observability, isolation attacks.

### S4 — cloud dual-mode worker

- Ownership:
  - `task-03-deployment-strategy/src/deployment_strategy/container.py`
  - `task-03-deployment-strategy/src/deployment_strategy/model_auth.py`
  - `task-03-deployment-strategy/terraform/modules/run_services/**`
  - `task-03-deployment-strategy/terraform/environments/dev/**`
  - `task-03-deployment-strategy/terraform/environments/production-model-plane/**`
  - `task-03-deployment-strategy/terraform/environments/production/**`
  - production/dev Terraform examples and Task 3 deployment docs/tests
- Deliverables:
  - explicit fake assessment, local Ollama, and cloud Ollama-compatible transport
    contracts (the product-facing runtime modes remain local and cloud);
  - worker env wiring for model URI/name/audience/search/logging;
  - one production Terraform composition connecting model and execution planes;
  - IAM and audience assertions.
- Interface consumed:
  - S2 runtime env names and validation semantics.
- Interface produced:
  - Terraform outputs expose worker model transport profile and model endpoint wiring
    without leaking private endpoints as public API outputs unless already present.
- Special note:
  - `terraform/environments/production` does not currently exist. Create it as the
    end-to-end opt-in composition that wires `model_plane` into `run_services`, while
    keeping `dev` as the fake assessment composition and `production-model-plane` as
    the standalone model-plane reference.
- Must not edit Task 1 retrieval or Task 2 worker internals.
- Dependencies: S2; consumes S2 runtime variable names and startup-validation
  semantics.
- Focused tests: container configuration, run_services tests, production-root tests.

### S5 — integration (root)

- Resolve narrow interface conflicts without broad refactors.
- Regenerate `uv.lock` once after all dependency edits.
- Use the reviewed `pypdf>=6,<7` range and test it through `AsyncLocalExtractor`.
- Add the integrated frozen end-to-end corpus and metrics that require S1+S2+S3 to
  exist together: source recall@k, chunk recall@k, max context size, deterministic
  ordering, duplicate handling, authority/freshness ties, and late-page citation
  provenance.
- Run focused suites in parallel, then full `make local-submission` and Terraform
  validation/tests.
- Run Terraform checks from a temporary clean `git archive`; do not modify ignored
  local `* 2.tf`/`* 2.hcl` files.
- Create atomic task commits and record the base/HEAD range.
- Dependencies: S1, S2, S3, S4.
- Go/no-go checks:
  - no raw content/prompt/secret strings in action trace or logs;
  - persisted trace/reflection payload remains bounded by the existing 64 KiB
    compatibility limit;
  - no cloud worker left in `AGENT_API_INFERENCE_MODE=fake` for the production
    Ollama composition;
  - no non-loopback HTTP model URL in local profile;
  - no provider fallback that multiplies query/fetch budgets;
  - no Task 2 or Task 3 import from Task 1 private retrieval internals except public
    executor/contracts.

### S6/S8 — adversarial review agents

- Test-and-report only.
- Review the exact base..HEAD task range, intended behavior, and commands.
- Probe SSRF/content bombs, malformed PDFs, context injection/tenant crossing, logging
  leaks, provider fallback budget amplification, cloud audience/IAM mismatch, and
  compatibility regressions.
- Leave reproduction tests uncommitted and list their paths.

### S7/S9 — remediation (root)

- Reproduce each confirmed finding, patch only production code and ordinary tests,
  preserve reviewer reproduction tests uncommitted, run focused/global verification,
  and create one atomic remediation commit per review cycle.
- The repository default says to present findings and wait, but the current user
  explicitly instructed this session to fix findings and repeat once. Follow the
  user's explicit instruction for this task; S9 completes after fixing the second
  report, and no third review is automatically launched.

## Numeric trace and retrieval bounds

- `ActionTraceRecord` max records per run: 128; extra records collapse into one
  `trace.truncated` record with counts by stage.
- String fields in trace records: stage/action/outcome/reason <= 64 ASCII chars;
  provider/format/profile <= 40 ASCII chars; safe IDs <= 80 ASCII chars.
- Persisted trace summary plus reflection must stay below 64 KiB serialized JSON.
- Logs may include pseudonymized tenant/session/run IDs using the existing telemetry
  HMAC key, but must not include raw request text, conversation text, raw URLs/domains,
  page text, prompts, hidden reasoning, credentials, exception bodies, or token values.
- PDF extraction supports text-bearing PDFs only. Encrypted, scanned/no-text,
  malformed, oversized, and excessive-content-stream PDFs fail with typed extraction
  reasons.
- PDF table support is conservative layout-preserved text: repeated nearby header and
  row text with page provenance, not a semantic spreadsheet parser.
- Context selection max: 8 selected chunks, 5 public quotes per evidence record, and
  a deterministic total selected-context character budget owned by Task 1 tests.

## Synchronization rules

- Workers announce interface changes before editing shared import seams.
- No worker stages, commits, resets, or deletes files.
- Existing user changes are preserved; unrelated files remain untouched.
- A worker that discovers a cross-package dependency reports it to root instead of
  expanding ownership silently.
- Root checks `git diff` by ownership before integration.
- Concurrency is capped at three workers plus root: Wave 1 runs S1/S2/S4; Wave 2 runs
  S3 only after S1 reports the frozen retrieval interface.

## Definition of done

- A frozen late-page fact in a PDF is selected and cited with page provenance.
- HTML/PDF table context retains headers and units.
- `auto`/fallback search survives a primary backend empty/failure outcome within the
  original budgets.
- A bounded same-session follow-up uses prior completed public context; cross-scope and
  hostile history do not.
- Every research stage emits reconstructable privacy-safe structured action data.
- Local loopback Ollama and private cloud Ollama-compatible service use the same agent;
  production Terraform wires cloud URI, model, audience, identity, and search config.
- Full Python/Terraform/submission gates pass.
- Both requested adversarial reports are complete and every confirmed finding from
  each has been fixed and verified.

## Required verification commands

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy task-*/src scripts
uv run pytest task-01-search-agent/tests task-02-agent-api/tests task-03-deployment-strategy/tests
uv run python task-01-search-agent/evals/run.py
uv run pytest tests/test_submission_audit.py
make local-submission
terraform -chdir=task-03-deployment-strategy/terraform/modules/run_services test
terraform -chdir=task-03-deployment-strategy/terraform/environments/dev test
terraform -chdir=task-03-deployment-strategy/terraform/environments/production-model-plane test
terraform -chdir=task-03-deployment-strategy/terraform/environments/production test
```
