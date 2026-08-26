# Task 4 data analysis

## Reproducible profile

Generated on August 26, 2026 from:

- `input/IT DA AI Tasks/Training_part1.csv`
- `input/IT DA AI Tasks/Training_part2.csv`

using `binary_classification.write_analysis(...)` after the validated D41 entity join.

## Observations

- The cleaned modeling table contains 3,700 entities and 17 candidate features.
- The target is imbalanced: `n=276`, `y=3424`. Accuracy alone will overstate model quality.
- `RAS` is the dominant missingness hotspot with 2,145 nulls. The next-largest missing counts are much smaller (`FAN=100`, `NUS=100`, `MYR=66`, `PKD=66`, `ERG=64`, `GJAH=64`, `UIN=39`, `KAT=39`).
- 3,424 rows share an identical feature vector with at least one peer. This is a data-property warning, not immediate leakage proof, but it means D43 must keep split logic entity-safe and report dispersion.

## Leakage screen

- `id` is the only quarantined identifier column.
- No non-identifier feature is deterministic with respect to the target in the deduplicated entity table.
- The largest class-conditional missingness gap is `RAS=0.05875`, which is notable but still far from a deterministic split.
- The strongest single-feature PR-AUC is `VOL=0.352429`. That is materially above the minority base rate (`276 / 3700 ≈ 0.0746`) but not implausibly perfect, so it stays available for D43 while remaining a feature to watch in model-card commentary.

## D43 implications

- Use PR-AUC as the primary ranking metric and always compare against a dummy baseline.
- Fit preprocessing inside training folds only; duplicated feature rows make leakage from pre-split transforms especially easy to hide.
- Keep `id` out of modeling inputs by construction.
