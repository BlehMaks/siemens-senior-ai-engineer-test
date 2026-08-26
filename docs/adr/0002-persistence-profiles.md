# ADR-0002: Separate local, assessment, and enterprise persistence

- Status: accepted
- Date: 2026-08-26

## Context

The solution needs deterministic local tests, a low-cost serverless deployment, and
a credible enterprise evolution. One database choice does not optimize all three.

## Decision

Use SQLite through behavior-level repository ports for local development. Use
Firestore for the assessment deployment. For enterprise control state, evaluate
Spanner when strongly consistent regional or multi-region topology is required and
AlloyDB/PostgreSQL when a regional relational workload is sufficient. Keep large
evidence bodies in object storage and add a vector store only after measured
semantic-retrieval demand.

The shared contracts fix tenant predicates, idempotency, ordering, leases,
cancellation, deletion, and consistency expectations before cloud adapters are
implemented.

## Alternatives

- Cloud SQL as the assessment baseline was rejected because an idle relational
  service adds cost and operations without proving an assignment requirement.
- Firestore as the universal production store was rejected because enterprise
  topology and transaction requirements must be discovered and measured.
- A lowest-common-denominator repository framework was rejected; adapters must meet
  behavior contracts without hiding backend differences.

## Consequences

Adapter contract tests are mandatory. Migration to another store changes adapter
code and operations, not Task 1 orchestration or the public API.

