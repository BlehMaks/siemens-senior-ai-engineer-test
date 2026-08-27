# Operations and privacy

The local API emits one-line JSON records through the standard Python logger
`agent_api.operations`. It deliberately has no vendor SDK dependency. A production
runtime can attach its approved logging handler and export the bounded metric
snapshot through the platform adapter selected in Task 3.

## Correlation and identity

Every response includes `X-Correlation-ID`. A valid client value is preserved;
otherwise the service creates an opaque value. Logs store only its stable HMAC
pseudonym, and correlation values never become metric labels. Tenant, session, and
run identifiers use the same telemetry-specific HMAC boundary. API-key identifiers,
the HMAC key, and raw identifiers are never emitted.

The typed telemetry methods do not accept arbitrary extra fields. Logs exclude
queries, prompts, reasoning, evidence, URLs, credentials, exception text, and PII.
Terminal run records contain only public state/failure codes, bounded Task 1 usage
counters, elapsed time, and pseudonymous identity. This is enough to join submission
and worker records without exposing user content.

HTTP authentication emits `auth.outcome` with only the bounded outcome, UTC time,
correlation pseudonym, and a tenant HMAC pseudonym when a key was verified. Unknown or
malformed credentials therefore disclose no claimed tenant or key identifier;
wrong-scope denials remain attributable through the verified tenant pseudonym.
The last-resort handler emits `request.unexpected_error` with only UTC time and the
correlation pseudonym, while the client receives the fixed generic error envelope.

## Bounded signals

The in-process metric snapshot has a finite key space. Its labels are limited to
run state, public failure code, usage resource, worker/lease/authentication outcome,
readiness outcome, and telemetry component. Correlation IDs and resource IDs can
never become labels. Counters cover submissions, cancellation requests, terminal
outcomes, policy failures, Task 1 usage, queue/worker outcomes, lease outcomes,
authentication, unexpected errors, readiness, and telemetry failures.

Durable audit entries record only the tenant scope, an idempotent pseudonymous event
ID, a bounded action such as `run.submitted` or `run.failed`, and a UTC timestamp.
Audit and log exporter failures are fail-open: they increment a bounded telemetry
failure counter but cannot change request or worker behavior.

## Health probes

- `GET /health/live` returns `200` while the process can serve HTTP.
- `GET /health/ready` verifies the migration ledger, physical SQLite schema, and
  stable file identity during its read-only snapshot. It returns
  `200 {"status":"ok",...}` when dependencies are usable and a bounded
  `503 {"status":"not_ready",...}` otherwise. A replacement after the completed
  snapshot is detected by the next probe; no filesystem probe can close that later
  race.

Readiness never returns table names, paths, exceptions, or storage diagnostics.
Telemetry exporter failure does not make the service unready. The local SQLite
probe is replaceable by a cloud dependency probe without changing the HTTP contract.
The database file and its parent directory are trusted operator-controlled storage;
the probe detects stable replacements and symlinks but is not a tamper boundary
against a local process that can atomically rewrite that directory during a check.

Once cancellation has durably made a run terminal, queue deletion is best-effort:
the request truthfully returns `202 changed=true`, while a retry or terminal worker
delivery removes stale dispatch. The terminal state prevents that dispatch from
executing and terminal telemetry is emitted exactly once for the applied transition.
