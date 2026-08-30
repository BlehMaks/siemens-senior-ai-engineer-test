# Task 4: Binary classification

## Assignment baseline

Join `Training_part1.csv` and `Training_part2.csv` on `id`, then develop a binary classifier for the `Class` target. Deliver the preprocessing description and model-development code.

## Observed data constraints

The supplied files use semicolon delimiters. Each has 4,070 rows but only 3,700 unique `id` values. Both files contain 370 exact duplicate rows, and neither has IDs missing from the other. A naive many-to-many merge would duplicate observations, so the pipeline must remove exact duplicates and validate a one-to-one join before modeling.

The positive/majority label appears 3,764 times and the minority label 306 times in the uncleaned second table. The features mix numeric values, low-cardinality categorical values, and substantial missingness. `RAS` is missing in 2,365 rows. These facts make accuracy alone misleading and require leakage checks before choosing a model.

## Assignment baseline approach

- `pandas` performs strict, transparent ingestion and the one-to-one entity join.
- Exact feature-vector groups stay together in both the final holdout and grouped CV.
- Fold-fitted `scikit-learn` pipelines compare a stratified dummy, logistic regression,
  and class-weighted logistic regression.
- PR-AUC is primary. The report also records dispersion, minority precision/recall,
  F1, ROC-AUC, calibration, cost-sensitive thresholds, and error slices.
- CatBoost has a documented early-exit decision in the model card; it cannot block the
  mandatory baseline.

The baseline remains the default. It does not fit a probability calibrator or apply
business decision costs unless an extension option is supplied.

## Business extension

The opt-in extension reuses the selected model's grouped out-of-fold probabilities.
It fits one model-agnostic sigmoid calibrator without reading holdout labels, then
chooses `class_0`, `manual_review`, or `class_1` by minimum expected cost. Review is
an explicit outcome, including on cost ties; it is never silently converted to a
class prediction.

Bundled scenarios are examples for demonstrating policy sensitivity. They are not
Siemens business truth. Use an owner-confirmed versioned JSON file for operational
interpretation:

```json
{
  "schema_version": "1.0",
  "scenarios": [
    {
      "name": "owner-confirmed",
      "false_positive_cost": 2.0,
      "false_negative_cost": 7.0,
      "review_cost": 0.5,
      "negative_label": "y",
      "positive_label": "n"
    }
  ]
}
```

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

## Run the assignment baseline

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
PR-AUC is `0.5626`; untouched-holdout PR-AUC is `0.4230`. The selected `0.6713`
threshold yields holdout recall `0.8545` and precision `0.4234`. Full results and
limitations are in `reports/model-card.md`; machine-readable aggregates are in
`reports/metrics.json`.

Training writes `selected-model.pkl`, containing the fitted preprocessing pipeline,
classifier, and schema. The evaluation command immediately reloads that exact
artifact and verifies prediction parity. Inference should load this trusted artifact
once and reuse it; the model is not retrained for each prediction. Re-run training
only when producing a new version from a new or deliberately changed dataset.

## Run the business extension

Select bundled examples explicitly:

```bash
uv run --locked python -m binary_classification.evaluate \
  --part1 "$SIEMENS_TASK4_INPUT_DIR/Training_part1.csv" \
  --part2 "$SIEMENS_TASK4_INPUT_DIR/Training_part2.csv" \
  --output-dir /tmp/task4-extension \
  --seed 42 \
  --cost-scenario balanced-review \
  --cost-scenario miss-averse-review
```

For owner-confirmed costs, replace the scenario flags with
`--cost-config /local/path/to/costs.json`. The extension writes
`baseline-vs-extension.json` and `baseline-vs-extension.md` from the same in-memory
result. The JSON records grouped-CV dispersion, baseline threshold metrics, raw and
calibrated Brier/log-loss diagnostics, scenario confusion counts, expected and
realized cost, automatic-decision coverage, review rate, and automatic error rate.
The trusted pickle also contains the calibrator and scenario configuration and is
reloaded immediately to verify raw and calibrated probability parity.

Failure is explicit: invalid, negative, non-finite, duplicate, wrongly labeled, or
unknown-version cost configurations stop the run. The holdout is used only for the
final comparison, never for model selection, sigmoid fitting, or policy setup.
Private rows are not written to the aggregate reports. Real costs, target meaning,
external validation, temporal drift, and production approval remain owner-owned
limitations.
