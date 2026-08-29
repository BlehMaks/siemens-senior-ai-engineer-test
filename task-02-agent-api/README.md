# Task 2: API for agent functionality

## Assignment baseline

Design and implement a REST API for the Internet-search agent. Users must be able to submit queries, receive responses, and retrieve the agent's current status.

Required deliverables are the API source code and API documentation with endpoints, request and response examples, usage instructions, and relevant design principles.

## Recommended stack

- FastAPI and Pydantic for an async, typed HTTP boundary and generated OpenAPI documentation.
- Uvicorn for local execution.
- A versioned `/v1` contract with opaque identifiers, idempotency keys, structured errors, and correlation IDs.
- SQLite for durable local users, sessions, runs, and memory metadata, accessed
  through repository ports with explicit transaction and concurrency semantics.
  The assessment deployment uses Firestore and Cloud Tasks through the same ports.
  Siemens-scale production evaluates Spanner for strongly consistent regional or
  multi-region control state, or AlloyDB/PostgreSQL when a regional relational model
  is the better fit. Add a vector index only after semantic-memory retrieval shows a
  measurable benefit.
- A queue abstraction for long-running research. Local tests may use an in-process adapter; the deployed implementation uses a durable managed queue from Task 3.
- Server-Sent Events for progress updates, with polling as a simple fallback. Full WebSockets are unnecessary unless bidirectional streaming becomes a measured requirement.
- `httpx` and `pytest` for contract and integration tests.

## Proposed API shape

- `POST /v1/sessions` creates a user-scoped research session.
- `POST /v1/sessions/{session_id}/runs` validates a query and returns `202 Accepted` with a run identifier.
- `GET /v1/runs/{run_id}` returns state, timestamps, bounded progress metadata, and the final answer when available.
- `GET /v1/runs/{run_id}/events` streams ordered status events.
- `POST /v1/runs/{run_id}/cancel` requests cancellation safely and idempotently.
- `GET /health/live` and `GET /health/ready` separate process health from dependency readiness.

The exact contract remains subject to implementation review. A synchronous convenience endpoint can be added for short requests, but it must not be the only interface to a long-running agent.

## Engineering extension

- User authentication with OIDC/JWT for interactive clients and separately managed API credentials for service clients.
- Tenant ownership enforced in every query, not only in URL routing.
- Per-principal and per-endpoint rate limits, concurrency budgets, request-size limits, and bounded model/search work.
- Durable state transitions with optimistic concurrency or an equivalent single-writer guarantee.
- User-scoped episodic, semantic, and procedural memory with retention and deletion controls.
- Audit events for authorization decisions, memory changes, tool use, and administrative actions.
- OpenTelemetry traces and metrics without recording secrets or full sensitive prompts by default.

## Constraints and acceptance checks

- Never keep authoritative job state only in a web-process dictionary; multiple instances and restarts must be safe.
- Authentication does not replace authorization. Tests must prove one user cannot read, stream, cancel, or influence another user's run or memory.
- API keys are generated with sufficient entropy, shown once, stored as hashes, scoped, rotated, and revocable.
- Validate JSON size, content type, query length, identifiers, pagination, and idempotency behavior before starting model work.
- Apply gateway limits and application-level work budgets. Rate limiting alone does not prevent expensive authenticated abuse.
- Define terminal and transitional states, including queued, running, waiting for tool, completed, failed, cancelled, and expired.
- Document retry semantics and ensure repeated client requests cannot create duplicate expensive runs.
- Threat-model SSRF, prompt injection, broken object-level authorization, mass assignment, resource exhaustion, injection, unsafe output handling, and sensitive-data leakage.

## Quota boundary

`QuotaLimiter` is the replaceable admission port. The local adapter uses SQLite
transactions for per-key token buckets, idempotency-keyed daily work admission,
queued-run accounting, and renewable execution/SSE leases. Actual ASGI request
bodies are pre-read only up to the configured ceiling and replayed for validation.
Accounting errors reject work with `503`; quota exhaustion returns a safe `429`
and integer `Retry-After`; oversized authenticated requests return `413`.

The assessment deployment keeps API-key hashes, quota guards and leases, and audit
entries in the same Firestore authority as run state. Successful lease acquisition
reclaims a bounded batch of expired execution or SSE leases, so crashed clients do
not leave an ever-growing collection. The production readiness probe performs a
read-only lookup against that shared store and never creates a local SQLite file.

Task 1 `RunBudget` remains the only counter for tool calls, model calls/attempts,
pages, raw/decoded bytes, tokens, iterations, and wall-clock timeout. The API does
not maintain a second copy of those counters.

The GCP target maps unauthenticated/global edge throttling to Cloud Armor or an API
gateway, dispatch/concurrency to Cloud Tasks queue controls, and durable work/SSE
accounting to Firestore transactions.

Operational logging, bounded metric dimensions, durable audit actions, correlation
behavior, and health-probe semantics are documented in
[`docs/operations.md`](docs/operations.md).

The local threat model and OWASP API 2023, ASVS 5.0, LLM 2025, and Agentic risk
decisions are recorded in [`docs/threat-model.md`](docs/threat-model.md).
