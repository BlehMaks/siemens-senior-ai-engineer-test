# ADR-0003: Keep memory application-managed and promotion-gated

- Status: accepted
- Date: 2026-08-28

## Context

The assignment asks for episodic, semantic, and procedural memory. A small local
model cannot safely own tenancy, provenance, retention, or runtime policy changes.

## Decision

Persist episodic retrospectives derived from observable typed events. Semantic facts
are citation-backed, tenant-scoped records with expiry, conflict identity, explicit
review, source invalidation, and deletion rules. Procedural playbooks are immutable,
versioned, declarative records; a human must review a version and separately select
it as active. Only human and deterministic-test authors are admitted.

The agent has one optional read-only adapter for approved, non-expired facts and
approved active procedures. It defaults off. When enabled, it runs only after search
and evidence collection, serializes a reduced bounded view into the synthesis user
message, and labels that view as untrusted background data. Memory never enters the
system prompt or planner, cannot select tools, and cannot relax evidence, citation,
URL, budget, or capability policy.

There is no LLM proposal, approval, activation, or procedure-execution path. Enabling
model-generated proposals requires a separately approved evaluation that measures
provenance accuracy, poisoning and prompt-precedence resistance, false conflicts,
expiry/deletion behavior, reviewer burden, and a useful quality delta on a frozen
suite. The excluded M5 benchmark provides none of that evidence.

Never store hidden reasoning, raw prompts, credentials, or full fetched pages as
memory.

## Alternatives

- Fine-tuning the model for memory was rejected because it does not provide precise
  per-user deletion, provenance, or deterministic access control.
- Automatic self-editing playbooks were rejected because model output cannot grant
  itself future capabilities.
- Omitting semantic and procedural seams was rejected because the requirements need
  an explicit safe extension path even when activation is deferred.

## Consequences

All three repository lifecycles ship independently of model quality. Existing runs
are byte-for-byte prompt-compatible because reviewed-memory reads default off. An
operator may opt into the read seam with reviewed records, but model-generated writes
and automatic promotion remain unavailable. Storage and session deletion own all
three memory families without giving the model a write capability.
