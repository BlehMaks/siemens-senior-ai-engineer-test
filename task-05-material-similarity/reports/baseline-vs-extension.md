# Task 5 baseline versus business extension

- Schema version: `1.1`
- Dataset fingerprint: `0000000000000000000000000000000000000000000000000000000000000000`
- Catalog rows: 8
- Blank descriptions: 1

## Mode comparison

| Mode | Status | Eligible/queries | Precision@5 | nDCG@5 | Exactly-five coverage |
|---|---|---:|---:|---:|---:|
| Lexical v1 | evaluated | 1 | 1.0 | 1.0 | 1.0 |
| Strict hybrid v2 | evaluated | 7 | 0.0 | 0.0 | 0.0 |
| Structured only v2 | evaluated | 1 | not evaluated | not evaluated | 0.0 |
| Relaxed hybrid v2.1 | evaluated | 7 | 1.0 | 1.0 | 1.0 |

## Safety and review workload

- Reviewed safety cases passed: 20/20
- Automatically rejected candidates: 0
- Candidates relaxed for engineering review: 11
- Cases requiring review: 7
- Cases without an evidence-backed result: 1

## Representative tolerance relaxations

- `Q` → `E`: current:numeric_hard_conflict (tolerance_only_relaxation_requires_engineering_review)
- `A` → `E`: current:numeric_hard_conflict (tolerance_only_relaxation_requires_engineering_review)
- `B` → `E`: current:numeric_hard_conflict (tolerance_only_relaxation_requires_engineering_review)

## Limitations

- Compatibility labels validate rule behavior; they do not certify electrical interchangeability.
- Structured-only precision@5 and nDCG@5 are not reported without reviewed relevance labels for blank-description rows.
- The batch API does not expose reliable per-query p50/p95 latency; those fields remain null.
- The bundled policy requires engineering-owner confirmation before operational use.
- Tolerance-only review can admit current-ratio and dimension-ratio conflicts; it never relaxes AC/DC, categorical, axis-count, contradictory, or unsupported evidence.
