# Task 2 threat model and security matrix

## Scope

This review covers the local FastAPI and SQLite submission profile. Task 3 owns
production ingress, TLS, IAM, secret management, Firestore, Cloud Tasks, and log
export. A concrete research executor is injected; when it is the Task 1 executor,
Task 1 owns public-web and model-provider controls.

The protected request path is:

```text
client -> bounded ASGI body -> API-key authentication and scope -> quota admission
       -> strict schema -> tenant-scoped service/repository -> durable queue
       -> leased worker -> injected executor -> public-output validation -> result
```

## Assets, actors, and boundaries

- Assets: tenant and key identity; key HMAC digests and pepper; sessions, queries,
  runs, events, memory reflections, work leases, quotas, audit records, and public
  answers; operational availability and correlation integrity.
- Remote actors: unauthenticated callers, callers holding a scoped tenant key, and
  a caller with a revoked or expired key. A key never grants another tenant's
  identity.
- Other untrusted inputs: search results, fetched pages, redirects, provider output,
  executor adapters, and persisted payloads read back from SQLite.
- Local actors: an operator controls the database path, pepper environment variable,
  key-admin CLI, filesystem permissions, and the injected executor.
- Public boundaries: health routes are unauthenticated and bounded. Every `/v1`
  object operation derives tenant identity from the authenticated principal; tenant
  identity is not accepted in public schemas.
- Durable boundary: repositories repeat tenant predicates, migrations verify the
  ledger and physical schema, run transitions use versions and leases, and the
  readiness probe validates the same read-only file descriptor whose identity is
  exposed at the configured path.
- Executor boundary: the worker passes tenant/session/run identity and a validated
  query to the executor, then rejects mismatched identity or unsafe public output.

## Security objectives and control owners

| Objective | Primary control | Verification owner |
| --- | --- | --- |
| Tenant and object isolation | Auth-derived tenant IDs, route scopes, tenant predicates, opaque IDs | `tests/routes/`, `tests/security/test_api_attack_surface.py`, repository contracts |
| Replay and race safety | Tenant-scoped idempotency, SQLite transactions, state/version CAS, renewable leases | run, quota, cancellation, queue, and worker tests |
| Bounded resource use | Body, request, queued/daily work, execution, SSE, Task 1 tool/model/page/byte/time budgets | `tests/security/test_limits.py`, quota route tests, Task 1 budget tests |
| Safe public output | Strict models, sensitive-text and public-URL validation, fixed failure messages, typed SSE encoding | schema, error, event, and SSE contract tests |
| Secret and privacy protection | HMAC key storage, scope/lifecycle checks, one-time key display, pseudonymous telemetry | auth, key-admin, observability, and redaction tests |
| Safe tool use | Task 1 URL guard, pinned connections, redirect/content/decompression limits, untrusted-evidence policy | Task 1 security and fetch tests |
| Durable recovery | Persist-before-dispatch, idempotent queue repair, terminal-state monotonicity, cancellation cleanup | service, storage, worker lifecycle, and cancellation tests |

## OWASP API Security Top 10 2023

| Risk | Decision and evidence |
| --- | --- |
| API1 Broken Object Level Authorization | Controlled: all object reads, streams, cancels, deletes, queue operations, and events use the authenticated tenant. Two-tenant negative route and repository tests cover the boundary. |
| API2 Broken Authentication | Controlled locally: 256-bit random keys, HMAC-SHA256 digests with a 32-byte pepper, constant-time comparison, bounded parsing, scopes, expiry, revocation, rotation, and active-key checks on SSE renewal. Production ingress and secret storage remain Task 3 controls. |
| API3 Broken Object Property Level Authorization | Controlled: strict request models forbid unknown fields; public response models omit tenant, key, lease, storage, prompt, and reasoning fields. Mass-assignment tests cover session and run bodies. |
| API4 Unrestricted Resource Consumption | Controlled: request bytes/rate, queued and daily work, execution and SSE leases, plus Task 1 tool/model/page/byte/token/time budgets fail closed. |
| API5 Broken Function Level Authorization | Controlled: sibling operations require separate `sessions:read`, `sessions:write`, `memory:delete`, `runs:read`, and `runs:write` scopes. |
| API6 Unrestricted Access to Sensitive Business Flows | Controlled: run creation is authenticated, quota-admitted, idempotent, asynchronous, and concurrency-limited; cancellation is terminal and idempotent. |
| API7 Server-Side Request Forgery | Task 2 performs no direct fetch. The reviewed Task 1 boundary rejects local/non-public addresses and unsafe ports, revalidates before connect, pins the socket, and bounds redirects and bytes. |
| API8 Security Misconfiguration | Partial by scope: strict errors, no debug surface, bounded health, schema verification, and safe defaults exist locally. TLS, ingress headers, IAM, service identities, and secret storage belong to Task 3. |
| API9 Improper Inventory Management | Controlled for this package: one versioned `/v1` surface and a checked OpenAPI snapshot. Deployment inventory and retirement policy remain Task 3/release work. |
| API10 Unsafe Consumption of APIs | Controlled at the imported Task 1 boundary: destination, status, redirect, content type, compression, size, extraction, evidence, provider output, and retry budgets are validated. |

## OWASP LLM Top 10 2025

| Risk | Decision and evidence |
| --- | --- |
| LLM01 Prompt Injection | Controlled at Task 1: page text is untrusted evidence, not instruction; tool choice and budgets remain code-owned. Injection-shaped API text remains data in the security suite. |
| LLM02 Sensitive Information Disclosure | Controlled: every public answer text channel and fixed error excludes sensitive material, reasoning, raw pages, credentials, tenant IDs, and internals; telemetry accepts only typed pseudonymous fields. |
| LLM03 Supply Chain | Partial: locked dependencies and repository checks exist. SBOM, image, provenance, and vulnerability gates are Task 3/release controls. |
| LLM04 Data and Model Poisoning | Partial: evidence is source-attributed, bounded, and validated; provider/model integrity and corpus governance are deployment decisions. |
| LLM05 Improper Output Handling | Controlled: executor identity and every public answer text channel are revalidated before persistence; citations accept public HTTP(S) URLs without credentials or sensitive query names. |
| LLM06 Excessive Agency | Controlled: the executor receives bounded research input and has only explicit search/fetch/model ports; Task 2 exposes no arbitrary tool or code-execution endpoint. |
| LLM07 System Prompt Leakage | Controlled: prompts and chain-of-thought are absent from HTTP/SSE/error/telemetry schemas. There is no public prompt-inspection route. |
| LLM08 Vector and Embedding Weaknesses | Not applicable: Task 2 has no vector or embedding store. Memory is tenant/session-scoped structured reflection storage. |
| LLM09 Misinformation | Mitigated, not eliminated: answers require validated citations and public evidence; factual quality remains an evaluation and model limitation rather than an authorization control. |
| LLM10 Unbounded Consumption | Controlled by layered API admission and the single Task 1 `RunBudget`; retries and SSE reconnects do not create fresh expensive work. |

## OWASP Agentic Security Initiative risks

| Risk | Decision and evidence |
| --- | --- |
| ASI01 Agent Goal Hijack | Task 1 treats external content as evidence and preserves the caller query and code-owned policy. |
| ASI02 Tool Misuse | Explicit tool ports, URL policy, content checks, and budgets constrain search/fetch/model use. |
| ASI03 Identity and Privilege Abuse | Tenant and key identity originate at auth; executor results must match the stored run identity. |
| ASI04 Agentic Supply Chain Vulnerabilities | Partial until Task 3/release SBOM, image signing, dependency, and provenance gates are complete. |
| ASI05 Unexpected Code Execution | No code, shell, template, plugin-install, or file-upload tool is exposed by Task 2. |
| ASI06 Memory and Context Poisoning | Reflections are tenant/session/run scoped, validated on read/write, and deletable; retrieval quality remains an evaluation concern. |
| ASI07 Insecure Inter-Agent Communication | Not applicable to Task 2: one worker calls one typed executor port; there is no peer-agent protocol. |
| ASI08 Cascading Failures | Bounded retries/timeouts, durable queue state, leases, cancellation, safe failures, and telemetry isolate dependency faults. |
| ASI09 Human-Agent Trust Exploitation | Mitigated by evidence/citations and bounded public states; UI disclosure and human approval policy are outside this API package. |
| ASI10 Rogue Agents | The worker cannot expand its tools or scopes and terminal writes require the claimed run/lease identity. |

## Relevant ASVS 5.0 traceability

The durable requirement-by-requirement working copy is
`security-audits/asvs-5.0/asvs-audit-checklist.md`. The principal application
controls map to ASVS V1.2.4 and V1.3.6 (injection/SSRF), V2.1.1, V2.1.3,
V2.2.1, V2.3.2-V2.3.4, and V2.4.1 (validation, transactions, automation),
V8.1.1-V8.1.2, V8.2.1-V8.2.3, V8.3.1-V8.3.2, and V8.4.1
(function, object, field, and cross-tenant authorization), V11.2.1, V11.4.3,
and V11.5.1 (HMAC and
randomness), V13.1.3, V13.2.4, and V13.3.1 (service budgets, outbound allowlists,
and deployment secret storage), V14.1.2 and V14.2.1-V14.2.4 (sensitive data),
V15.1.3, V15.2.2, V15.3.2, and V15.4.2 (availability, redirects, and races), and
V16.1.1-V16.5.4 (logging and safe failures).

## Accepted limitations and follow-up

- Global unauthenticated throttling, DDoS absorption, TLS, security headers, service
  identities, pepper storage/rotation, encrypted storage, log retention/access, and
  backup controls require the Task 3 deployment evidence. Local code must not claim
  those controls.
- Task 2 accepts an injected executor. A deployment must select the reviewed Task 1
  implementation or demonstrate equivalent SSRF, prompt/tool, and budget controls.
- Source-backed citations reduce, but cannot eliminate, model misinformation or
  malicious-source risk. Task 1 evaluation owns measured answer quality.
- SQLite is the offline assessment store, not the Siemens-scale multi-region target.
  Its parent-directory permissions and backup handling are operator controls.
- FastAPI decodes a size-bounded malformed JSON body before route dependencies and
  therefore returns the same generic `422` envelope with or without a key. This path
  cannot reach tenant data, quota identity, model work, or persistence. Source-IP
  throttling for unauthenticated parser load remains an explicit ingress control;
  duplicating route/auth policy in middleware was rejected as disproportionate.

Reference sets: [OWASP API Security Top 10 2023](https://owasp.org/API-Security/editions/2023/en/0x11-t10/),
[OWASP ASVS 5.0](https://github.com/OWASP/ASVS/releases/tag/v5.0.0),
[OWASP LLM Top 10](https://genai.owasp.org/llm-top-10/), and
[OWASP Agentic Security Initiative](https://genai.owasp.org/initiatives/agentic-security-initiative/).

The sealed Codex Security result is summarized in
[`security-audits/codex-standard/report.md`](../security-audits/codex-standard/report.md).
