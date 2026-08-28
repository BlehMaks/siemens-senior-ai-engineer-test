# Task 1: Internet-search agent

## Assignment baseline

Build an LLM-powered agent that can answer questions beyond the model's embedded knowledge by using an internet search tool. The supplied scenario focuses on in-depth research into company sustainability reports.

The agent must:

- use an LLM, with Ollama or a Llama 3.1 repository given as examples;
- integrate a search engine such as DuckDuckGo;
- decide when a web search is appropriate;
- avoid unnecessary searches for greetings, simple queries, and context-based replies;
- interpret search results and return a human-readable answer.

Required deliverables are a working agent and documentation of the approach and findings.

## Recommended stack

- Python 3.12 and `uv` for a reproducible local environment.
- Ollama as the portable local inference runtime. The model stays behind a small provider interface so the same agent can use an optimized Apple Silicon runtime or a managed cloud endpoint without changing orchestration logic.
- A typed state graph for routing, searching, extracting, synthesizing, and verifying. LangGraph is appropriate only if its checkpointing and replay features are used; otherwise a small explicit loop is preferable.
- Pydantic models for tool arguments, state transitions, citations, and persisted memory records.
- DuckDuckGo search for discovery, `httpx` for bounded HTTP retrieval, and a dedicated main-content extractor for static pages.
- Playwright only as a controlled fallback for pages that require a browser. It is not the default fetch path.
- SQLite for local checkpoints and memory. Repository ports keep Task 1 independent
  of the deployed store: Task 2 retains SQLite locally, Task 3 uses Firestore in the
  assessment environment, and production can select Spanner or AlloyDB/PostgreSQL
  without changing the agent.
- `pytest` plus a versioned behavior-evaluation set covering routing, source quality, grounding, safety, and failure handling.

The final local model is an evaluation result, not a documentation preference. Candidate models must support reliable structured output or tool calling at an acceptable latency on the MacBook Pro M5 with 48 GB memory. Record quality, time to first token, generation rate, peak memory, context size, quantization, and license before selecting one.

## Engineering extension

The production-oriented version adds:

- a bounded research harness that plans only the searches needed for the request;
- source citations and claim-to-source grounding checks;
- URL and content guardrails, prompt-injection isolation, search/fetch budgets, and explicit refusal reasons;
- optional browser navigation under the same policy as direct fetching;
- episodic memory for one research run, semantic memory for verified user-scoped facts, and procedural memory for reviewed playbooks;
- traceable agent states so Task 2 can expose meaningful progress rather than a generic busy flag.

Memory is external state, so a local model does not prevent its implementation. The
implemented lifecycle is deliberately model-independent:

- semantic candidates carry tenant, source, evidence, conflict, expiry, and origin
  identities; only an explicit review can approve them;
- procedures contain bounded declarative text, retain immutable version history, and
  require both review and an explicit active-version selection;
- source, session, procedure, fact, and tenant deletion are durable, and SQLite
  reopen preserves lifecycle state and consumed procedure version numbers;
- `RepositoryReviewedMemoryReader` exposes at most eight active facts and four active
  procedures to answer synthesis. `ResearchRunner.memory_reads_enabled` is `False`
  by default, and disabled runs do not call the reader or add a prompt field;
- enabled memory is reduced to public fact provenance and declarative steps, appears
  only under `reviewed_memory_untrusted_data`, and cannot change planning, tools,
  capabilities, system instructions, budgets, or citation validation.

There is no model proposal, automatic approval, automatic activation, or executable
procedure path. Those paths remain unavailable because the excluded model benchmark
does not establish proposal quality. See the
[memory threat model](docs/memory-threat-model.md),
[memory evaluation notes](docs/memory-evaluation.md), and
[ADR-0003](../docs/adr/0003-application-managed-memory.md).

## Constraints and acceptance checks

- This machine is not a valid target for local-model performance tests. Run model benchmarks on the specified M5/48 GB machine and keep hardware-dependent results separate from functional tests.
- Search results and fetched pages are untrusted. A page cannot change system policy or tool permissions.
- Fetching must allow only HTTP(S), resolve and re-check redirects, block private/link-local/metadata endpoints, cap bytes and duration, and reject unsupported content types.
- The agent must cite the sources used for factual claims and distinguish unavailable evidence from a confident negative answer.
- The search router must be evaluated on both search-needed and no-search cases.
- Tool failures, empty results, conflicting sources, duplicate results, timeouts, and cancellation must produce defined states.
- Memory is scoped to an authenticated user/session contract even before Task 2 supplies the transport layer.
- The model license must permit the intended local demonstration and proposed deployment.

## Verify

Run the deterministic 34-case behavior evaluation and the Task 1 tests from the
repository root:

```bash
uv run --locked python task-01-search-agent/evals/run.py --mode fixed
uv run --locked pytest -q task-01-search-agent/tests tests
```

The fixed evaluation covers routing, terminal budgets, prompt and raw-page
disclosure, private-address blocking, citation fidelity, and abstention. Live Ollama
captures are optional and follow [the frozen model-selection protocol](docs/model-selection.md).
The checked `evals/fixtures/reviewed-memory.json` before/after fixture additionally
proves the default-off prompt shape, bounded opt-in payload, and deterministic reader
call count. Memory repository, corruption, prompt-precedence, and deletion cases run
with the Task 1 tests above and the Task 2 storage suite.
