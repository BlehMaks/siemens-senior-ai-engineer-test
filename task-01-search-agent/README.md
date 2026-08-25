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
- SQLite for local checkpoints and memory, with a storage interface that can move to PostgreSQL in Task 2.
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

Memory is external state, so a local model does not prevent its implementation. The constraint is quality: model-generated summaries and playbooks must be validated before they can influence future runs. Procedural memory must be versioned and promoted through an explicit review step instead of allowing the agent to rewrite its own operating rules silently.

## Constraints and acceptance checks

- This machine is not a valid target for local-model performance tests. Run model benchmarks on the specified M5/48 GB machine and keep hardware-dependent results separate from functional tests.
- Search results and fetched pages are untrusted. A page cannot change system policy or tool permissions.
- Fetching must allow only HTTP(S), resolve and re-check redirects, block private/link-local/metadata endpoints, cap bytes and duration, and reject unsupported content types.
- The agent must cite the sources used for factual claims and distinguish unavailable evidence from a confident negative answer.
- The search router must be evaluated on both search-needed and no-search cases.
- Tool failures, empty results, conflicting sources, duplicate results, timeouts, and cancellation must produce defined states.
- Memory is scoped to an authenticated user/session contract even before Task 2 supplies the transport layer.
- The model license must permit the intended local demonstration and proposed deployment.
