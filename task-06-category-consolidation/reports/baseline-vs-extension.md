# Task 6 baseline versus business extension

Schema version: `1`

Fixture fingerprint: `sha256:7dc26bf7160f8271368725050fdf19591635bab39753f934ff7e20815670b81b`

Training/inference rows: `12` / `6`

## Assignment baseline

Standalone single-column percentage-only helper.

Single-column output equivalence with the percent-only adapter: `true`.

| Column | Categories before/after | Fallback count/rate | Unseen count/rate | Retained |
|---|---:|---:|---:|---:|
| `channel` | 5 / 4 | 2 / 0.333 | 1 / 0.167 | 3 |
| `region` | 6 / 4 | 3 / 0.500 | 1 / 0.167 | 3 |

## Business extension

Opt-in multi-column percentage plus minimum-count policy.

| Column | Categories before/after | Fallback count/rate | Unseen count/rate | Retained |
|---|---:|---:|---:|---:|
| `channel` | 5 / 3 | 3 / 0.500 | 1 / 0.167 | 2 |
| `region` | 6 / 3 | 4 / 0.667 | 1 / 0.167 | 2 |

The safe mapping artifact uses schema version `1` with fingerprint `sha256:b2506f3ca431a877c509b759a1f60651a05be9fb5ce16dc8a8ec9408f6789924`.

All recorded sklearn checks passed: `true`. Alias normalization is `not_implemented`.

## Bounded microbenchmark

Rows / iterations: `5000` / `5`. Median core time: `0.015647s`; median adapter time: `0.014721s`; peak memory: `254232` bytes.

These measurements are bounded engineering evidence, not a universal performance promise.

## Limitations

- The fixture is sanitized engineering evidence, not production data.
- Runtime and peak memory vary by machine and are not universal promises.
- Category meaning is not inferred; alias normalization remains deferred.
