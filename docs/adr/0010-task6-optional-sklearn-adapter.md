# ADR 0010: Keep the Task 6 sklearn adapter optional

## Context

The assignment asks for a lightweight standalone rare-category function. Production
pipelines also benefit from a fitted multi-column DataFrame transformer and a safe,
portable mapping artifact.

## Decision

Keep the core module free of pandas and sklearn. Add optional `min_count` without
changing the percentage-only default. Place sklearn conventions in a separate
adapter that owns one fitted core consolidator per selected column. Serialize a
strict JSON-safe scalar vocabulary and reject unsupported or unknown artifacts.
See the [adapter diagram](../diagrams/task6-sklearn-adapter.mmd).

## Rejected alternatives

- Making pandas and sklearn mandatory weakens the standalone assignment contract.
- Pickle is not an acceptable portable mapping exchange format because loading it
  can execute code.
- Fuzzy category matching or target-driven aliases add unreviewed semantics.

## Consequences

Base and optional-extra installations need separate tests. DataFrame index, order,
non-selected columns, feature names, cloning, and pandas output are explicit
contracts. Each fitted mapping reports unseen categories and a stable fingerprint.
