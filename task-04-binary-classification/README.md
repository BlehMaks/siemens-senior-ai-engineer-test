# Task 4: Binary classification

## Assignment baseline

Join `Training_part1.csv` and `Training_part2.csv` on `id`, then develop a binary classifier for the `Class` target. Deliver the preprocessing description and model-development code.

## Observed data constraints

The supplied files use semicolon delimiters. Each has 4,070 rows but only 3,700 unique `id` values. Both files contain 370 exact duplicate rows, and neither has IDs missing from the other. A naive many-to-many merge would duplicate observations, so the pipeline must remove exact duplicates and validate a one-to-one join before modeling.

The positive/majority label appears 3,764 times and the minority label 306 times in the uncleaned second table. The features mix numeric values, low-cardinality categorical values, and substantial missingness. `RAS` is missing in 2,365 rows. These facts make accuracy alone misleading and require leakage checks before choosing a model.

## Implemented approach

- `pandas` performs strict, transparent ingestion and the one-to-one entity join.
- Exact feature-vector groups stay together in both the final holdout and grouped CV.
- Fold-fitted `scikit-learn` pipelines compare a stratified dummy, logistic regression,
  and class-weighted logistic regression.
- PR-AUC is primary. The report also records dispersion, minority precision/recall,
  F1, ROC-AUC, calibration, cost-sensitive thresholds, and error slices.
- CatBoost has a documented early-exit decision in the model card; it cannot block the
  mandatory baseline.

## Constraints and acceptance checks

- Assert delimiter, schema, target values, ID uniqueness after deduplication, and one-to-one join cardinality.
- Fit imputation, encoding, scaling, and feature selection only on training folds.
- Detect exact and near-deterministic feature relationships before training; remove leakage rather than celebrating an implausible score.
- Compare against a dummy model and a simple linear model before accepting a more complex learner.
- Handle imbalance through metrics, threshold selection, class weights, or sampling inside training folds. Never resample before the split.
- Report cross-validation dispersion and confidence limitations, not only a best score.
- Fix seeds where possible and serialize the complete preprocessing-plus-model pipeline.
- Keep an executable training command and focused tests for the join and transformation invariants.
- On the recorded reference machine, the mandatory CPU baseline and evaluation
  target at most five minutes and 2 GB peak memory. CatBoost exploration stops after
  20 declared configurations or 15 minutes, whichever comes first.

## Run

```bash
uv sync --all-packages --all-groups --locked
uv run --locked python -m binary_classification.evaluate \
  --part1 "$SIEMENS_TASK4_INPUT_DIR/Training_part1.csv" \
  --part2 "$SIEMENS_TASK4_INPUT_DIR/Training_part2.csv" \
  --output-dir /tmp/task4-run \
  --seed 42
uv run --locked pytest -q task-04-binary-classification/tests
```

The recorded seed-42 run selected weighted logistic regression. Its grouped-CV mean
PR-AUC is `0.5792`; untouched-holdout PR-AUC is `0.4230`. The selected `0.6700`
threshold yields holdout recall `0.8545` and precision `0.4234`. Full results and
limitations are in `reports/model-card.md`; machine-readable aggregates are in
`reports/metrics.json`.
