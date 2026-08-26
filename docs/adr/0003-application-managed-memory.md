# ADR-0003: Keep memory application-managed and promotion-gated

- Status: accepted
- Date: 2026-08-26

## Context

The assignment asks for episodic, semantic, and procedural memory. A small local
model cannot safely own tenancy, provenance, retention, or runtime policy changes.

## Decision

Persist episodic retrospectives derived from observable typed events as the baseline.
Define semantic facts as evidence-linked, tenant-scoped records with confidence,
expiry, conflict, and deletion rules. Define procedural playbooks as signed or
versioned human-reviewed artifacts. Model-generated facts and procedures remain
staged and disabled until named evaluation and approval gates pass.

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

Episodic memory ships independently of model quality. Semantic and procedural write
paths may remain disabled without weakening the mandatory agent.

