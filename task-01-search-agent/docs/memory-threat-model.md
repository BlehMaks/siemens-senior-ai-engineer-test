# Reviewed memory threat model

## Boundary and assets

Semantic facts and procedures are durable application data, not trusted model
instructions. The protected assets are tenant isolation, source provenance, review
state, expiry, immutable procedure history, active-version selection, deletion, and
the runner's code-owned prompt/tool/citation policy.

The only runtime read flow is:

```text
tenant-scoped repositories -> approved/active bounded view
  -> strict reconstruction -> synthesis user-data field -> cited answer validation
```

The read is disabled by default and occurs after planning, search, fetching, and
evidence construction. It therefore cannot grant a tool, cause a navigation, raise a
budget, replace a system message, or become citation evidence.

## Threats and controls

| Threat | Control and deterministic evidence |
| --- | --- |
| Cross-tenant record or forged scope | Every repository repeats tenant predicates; duplicated SQLite metadata is decoded and compared; `ReviewedMemoryContext` requires one exact tenant. Two-tenant and corruption tests fail closed. |
| Unreviewed, rejected, expired, or inactive data | Semantic reads require `approved` and `expires_at > observed_at`; procedural reads follow only approved active pointers. The context reconstructs every exact record and rejects future reviews. |
| Prompt or capability injection | Admission rejects control phrases, hidden prompts, code-execution forms, credentials, private URLs, and invisible separator tricks. The reduced view is in a named untrusted user-data field; the system prompt says it is neither evidence nor instruction. Forged post-validation models fail before the provider call. |
| Automatic self-modification | The reader exposes no write method. Proposal, review, and active-version selection are separate repository calls, and only human or deterministic-test authors exist. Procedures are never executed. |
| Conflict or stale fact | Approval rejects another active conflict identity at the review time. Expiry uses an exact boundary; source and session deletion remove derived facts. |
| History rewrite or rollback abuse | Procedure versions are sequential and immutable, version heads prevent reuse after deletion, compare-and-swap detects races/ABA, and rollback only selects an already approved version. |
| Corrupt persisted state hidden by indexes | Reads and destructive operations revalidate exact models, physical/JSON identity, heads, active pointers, and schema constraints. Mutations roll back on corruption. |
| Secret or private-data retention | Bounded models reject credentials, sensitive query fields, private/non-public URLs, raw pages, prompts, reasoning, exceptions, and unknown fields. The synthesis view omits reviewer and origin identifiers. |
| Deletion cache lag | The repository reader has no cache. Task 2 session/tenant deletion owns reflection, fact, procedure, pointer, and head lifecycle; the next read observes deletion. |
| Resource exhaustion | Counts are capped at eight facts and four procedures, text/collections/serialized records are bounded, and prompt bytes are charged to the existing model-token budget. |

## Trust and availability trade-off

The SQLite adapter fails closed when durable identity can no longer be attributed
safely. This can make a corrupted local database unavailable; silently returning a
different tenant's or unaudited procedure data is not an acceptable fallback. File
integrity, backup, encryption, and operator access remain deployment controls.

## Disabled paths and enablement evidence

Model-generated semantic/procedural proposals, automatic review, automatic
activation, and procedure execution do not exist. Enabling model proposals requires
a frozen, tenant-safe evaluation with independently reviewed provenance accuracy,
conflict/expiry/deletion correctness, prompt-poisoning resistance, reviewer burden,
and a predeclared useful answer-quality delta. A model speed benchmark or anecdotal
examples are insufficient.
