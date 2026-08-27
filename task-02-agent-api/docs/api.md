# Research Agent API contract

This document freezes the public HTTP and event-stream contract for API v1. It
does not define authentication, persistence, queue processing, or route
implementations. All identifiers are opaque strings; clients must not parse
meaning from them.

## HTTP surface

| Method | Path | Success | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health/live` | `200` | Process liveness |
| `GET` | `/health/ready` | `200` | Dependency readiness |
| `POST` | `/v1/sessions` | `201` | Create a session |
| `GET` | `/v1/sessions` | `200` | List sessions with cursor pagination |
| `GET` | `/v1/sessions/{session_id}` | `200` | Read one session |
| `DELETE` | `/v1/sessions/{session_id}` | `204` | Delete one session |
| `DELETE` | `/v1/sessions/{session_id}/memory` | `200` | Delete persisted session memory |
| `POST` | `/v1/sessions/{session_id}/runs` | `202` | Submit asynchronous work |
| `GET` | `/v1/runs/{run_id}` | `200` | Read run status or result |
| `POST` | `/v1/runs/{run_id}/cancel` | `202` | Request cancellation |
| `GET` | `/v1/runs/{run_id}/events` | `200` | Resume a typed SSE stream |

Health endpoints are intentionally unversioned. The application API is under
`/v1`; incompatible public changes require a new version prefix.

## Request headers

- `X-Correlation-ID` is optional on every operation. It is an opaque ID used
  for support correlation. A generated or accepted correlation ID is returned
  in the safe error envelope.
- `Idempotency-Key` is required when submitting a run. It is an opaque,
  tenant-scoped retry key. Repeating the same request with the same key must
  resolve to the same accepted run; conflicting reuse is a `409` response.
- `Last-Event-ID` is optional on the event stream. It is the positive decimal
  sequence of the last event processed by the client. The server resumes with
  events whose sequence is greater than that value.

Header values reject whitespace, signs, padding, and unbounded numeric values.
Neither an idempotency key nor a cursor carries tenant or database meaning.

## Sessions and pagination

Create session accepts an optional label:

```json
{"label":"Compressor research"}
```

Session responses contain only `session_id`, the optional label, and UTC
creation/update timestamps. Listing accepts `limit=1..100` (default `50`) and
an optional opaque `cursor`. Results contain at most 100 items and an optional
`next_cursor`; clients must pass that cursor back unchanged.

Deleting a session returns no body. Deleting session memory returns a bounded
`deleted_count` and a UTC `completed_at` timestamp. The application layer must
apply the authenticated tenant predicate to every read and deletion; tenant
identity is never supplied or returned in an HTTP body.
An absent resource and a resource outside the authenticated tenant scope return
the same `404` envelope so identifiers cannot be enumerated.

## Runs

Run submission accepts one bounded query:

```json
{"query":"Which documented evidence supports the proposed answer?"}
```

The `202` response contains an opaque session ID, opaque run ID, `queued`
state, and UTC creation time. A status response exposes lifecycle timestamps,
a cancellation flag, and exactly one public terminal result where applicable.
The public answer reuses the Task 1 `ScopedAnswer` and `Citation` types and is
additionally limited to 16 citations and eight follow-up queries.

Valid terminal response shapes are:

```json
{
  "session_id":"session-one",
  "run_id":"run-one",
  "state":"completed",
  "created_at":"2026-08-27T10:00:00Z",
  "updated_at":"2026-08-27T10:00:02Z",
  "terminal_at":"2026-08-27T10:00:02Z",
  "cancellation_requested":false,
  "answer":{
    "answer_text":"The documented answer is supported by the cited source.",
    "citations":[{
      "claim":"The source supports the answer.",
      "evidence_id":"ev-source-one",
      "source_url":"https://example.com/source"
    }],
    "assistance":null
  },
  "failure":null
}
```

```json
{"state":"failed","answer":null,"failure":{"code":"execution_failed","message":"Run failed within the configured boundary.","retryable":false}}
```

```json
{"state":"cancelled","cancellation_requested":true,"answer":null,"failure":null}
```

```json
{"state":"expired","answer":null,"failure":{"code":"expired","message":"Run expired before work could complete.","retryable":true}}
```

The abbreviated failed, cancelled, and expired examples retain the same ID and
timestamp fields as the complete example. A completed run has an answer and no
failure. Failed and expired runs have a safe failure and no answer. Cancelled
runs expose neither. Non-terminal states expose neither.

Cancellation reports the observed run state, whether cancellation is now
requested, whether this request changed that flag, and the public request time.
It does not claim that running work has stopped synchronously.

## Errors

All documented JSON error responses use one envelope:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "The request could not be accepted.",
    "correlation_id": "correlation-one",
    "retryable": false,
    "field_issues": [
      {"field": "query", "message": "Use a query between 3 and 240 characters."}
    ]
  }
}
```

Documented codes are `invalid_request`, `unauthenticated`, `forbidden`,
`not_found`, `conflict`, `rate_limited`, `unavailable`, and `internal_error`.
The envelope is used for `400`, `401`, `403`, `404`, `409`, `422`, `429`,
`500`, and `503` responses where applicable. Messages are bounded and safe for
clients. They never contain tenant IDs, prompts, chain-of-thought, raw pages,
credentials, traceback text, exception types, or storage/provider internals.

## Server-sent events

The stream media type is `text/event-stream`. Each application event is one
UTF-8 frame with a decimal sequence ID, a typed event name, and a single JSON
data line:

```text
id: 7
event: run.status
data: {"schema_version":1,"sequence":7,"run_id":"run-one","event_type":"run.status","state":"running","occurred_at":"2026-08-27T10:00:00Z","message":"Run state changed."}

```

Event names are `run.status`, `run.completed`, `run.failed`, `run.cancelled`,
and `run.expired`. Terminal event payloads follow the same answer/failure rules
as status responses. Status events are non-terminal. Newlines and carriage
returns inside messages are JSON-escaped, so untrusted text cannot create SSE
fields. A keepalive contains only the SSE comment `: heartbeat` followed by a
blank line and therefore does not advance the resume cursor.

Delivery can be duplicated. Clients must process events in ascending sequence
order and deduplicate by `(run_id, sequence)`. Reconnect with the last fully
processed sequence in `Last-Event-ID`; a missing header starts at the earliest
available event. The exact retention window and unavailable-history response
are lifecycle implementation decisions, not part of this schema-only unit.

## Information boundary

Public schemas forbid extra fields. They expose no authenticated tenant ID,
prompt, chain-of-thought, raw page, raw provider response, internal exception,
traceback, lease, worker, queue, database, or storage identifier. Authorization,
tenant scoping, idempotent persistence, event retention, and cancellation
execution are deliberately deferred to later implementation units.
