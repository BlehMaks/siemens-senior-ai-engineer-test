# Task 5 baseline versus business extension

- Schema version: `1.0`
- Dataset fingerprint: `0000000000000000000000000000000000000000000000000000000000000000`
- Catalog rows: 8
- Blank descriptions: 1

## Mode comparison

| Mode | Status | Eligible/queries | Precision@5 | nDCG@5 | Exactly-five coverage |
|---|---|---:|---:|---:|---:|
| Lexical v1 | evaluated | 1 | 1.0 | 1.0 | 1.0 |
| Strict hybrid v2 | evaluated | 7 | 1.0 | 1.0 | 1.0 |
| Structured only v2 | evaluated | 1 | not evaluated | not evaluated | 0.0 |
| Relaxed hybrid | not_implemented | 0 | not evaluated | not evaluated | not evaluated |

## Safety and review workload

- Reviewed safety cases passed: 20/20
- Automatically rejected candidates: 0
- Cases requiring review: 0
- Cases without an evidence-backed result: 1

## Limitations

- Compatibility labels validate rule behavior; they do not certify electrical interchangeability.
- Structured-only precision@5 and nDCG@5 are not reported without reviewed relevance labels for blank-description rows.
- The batch API does not expose reliable per-query p50/p95 latency; those fields remain null.
- The bundled policy requires engineering-owner confirmation before operational use.
