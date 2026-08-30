# ADR 0009: Separate Task 5 compatibility gates from similarity scores

## Context

Description similarity can retrieve useful alternatives but cannot compensate for
an explicit electrical or dimensional conflict. Missing, unsupported, and
contradictory evidence require different outcomes.

## Decision

Reuse the deterministic TF-IDF ranker and existing bounded field parsers. Apply a
versioned, owner-reviewed compatibility policy before soft ranking. Hard conflicts
exclude candidates. Blank descriptions may enter an explicitly named
structured-only mode only above configured evidence thresholds. See the
[compatibility diagram](../diagrams/task5-compatibility-policy.mmd).

## Rejected alternatives

- Embeddings or ANN infrastructure do not solve compatibility and are unnecessary
  for the current catalog size.
- A single blended score can hide hard conflicts.
- Fabricating five candidates converts missing evidence into false confidence.

## Consequences

Results explain accepted, excluded, unsupported, review-required, and
insufficient-evidence outcomes. They support engineering review and never certify
electrical interchangeability. The lexical version-1 default remains unchanged.
