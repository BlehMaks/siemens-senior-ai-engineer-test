# Task 4: Binary classification

## Assignment baseline

Join `Training_part1.csv` and `Training_part2.csv` on `id`, then develop a binary classifier for the `Class` target. Deliver the preprocessing description and model-development code.

## Observed data constraints

The supplied files use semicolon delimiters. Each has 4,070 rows but only 3,700 unique `id` values. Both files contain 370 exact duplicate rows, and neither has IDs missing from the other. A naive many-to-many merge would duplicate observations, so the pipeline must remove exact duplicates and validate a one-to-one join before modeling.

The positive/majority label appears 3,764 times and the minority label 306 times in the uncleaned second table. The features mix numeric values, low-cardinality categorical values, and substantial missingness. `RAS` is missing in 2,365 rows. These facts make accuracy alone misleading and require leakage checks before choosing a model.

## Recommended stack and approach

- `pandas` for transparent ingestion, validation, deduplication, and joining. The dataset is too small to justify a distributed engine.
- `scikit-learn` pipelines for a dummy baseline and a regularized logistic-regression baseline with imputation and one-hot encoding.
- CatBoost as the primary nonlinear candidate because it handles mixed categorical/numeric data and missing values with limited preprocessing.
- Stratified cross-validation on the deduplicated entity set, with a final untouched holdout if the sample size supports both.
- PR-AUC as the primary imbalance-sensitive ranking metric, accompanied by ROC-AUC, confusion matrices, minority recall/precision, F1, calibration, and a documented operating threshold.

## Constraints and acceptance checks

- Assert delimiter, schema, target values, ID uniqueness after deduplication, and one-to-one join cardinality.
- Fit imputation, encoding, scaling, and feature selection only on training folds.
- Detect exact and near-deterministic feature relationships before training; remove leakage rather than celebrating an implausible score.
- Compare against a dummy model and a simple linear model before accepting a more complex learner.
- Handle imbalance through metrics, threshold selection, class weights, or sampling inside training folds. Never resample before the split.
- Report cross-validation dispersion and confidence limitations, not only a best score.
- Fix seeds where possible and serialize the complete preprocessing-plus-model pipeline.
- Keep an executable training command and focused tests for the join and transformation invariants.
