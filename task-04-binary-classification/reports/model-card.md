# Task 4 model card

## Decision summary

The selected model is class-weighted logistic regression. It predicts the minority
label `n`; `y` is the majority label. Selection used mean grouped-CV PR-AUC, not
accuracy. The operating threshold is `0.670003`, chosen from out-of-fold training
predictions under an illustrative 5:1 false-negative to false-positive cost ratio.

This is a reproducible baseline, not a production approval. The anonymized features
and unspecified business costs prevent a defensible claim that the selected threshold
matches a real operational loss function.

## Data and leakage controls

The loader reads both semicolon-delimited files, removes only exact source rows,
rejects conflicting IDs, and enforces a one-to-one join. The supplied 4,070-row files
produce 3,700 entities; a naive raw merge would produce 4,475 rows.

Only 490 distinct complete feature vectors exist. There are 214 repeated vectors,
covering 3,424 rows, with up to 16 entities in one group. Identical vectors never
cross the holdout or CV boundary. The fixed seed-42 split contains 2,957 training rows
and 743 holdout rows. The holdout is evaluated only after candidate and threshold
selection.

`id` is quarantined. Numeric columns use fold-fitted median imputation, missingness
indicators, and scaling. Categorical columns use a fold-fitted missing sentinel and
one-hot encoding with unseen-category tolerance. No imputer, encoder, scaler, model,
or threshold is fitted on validation or holdout rows.

## Candidate comparison

Five-fold `StratifiedGroupKFold` results on the training partition:

| Candidate | Mean PR-AUC | Fold SD | OOF PR-AUC | OOF recall at 0.5 | OOF precision at 0.5 |
|---|---:|---:|---:|---:|---:|
| Stratified dummy | 0.0773 | 0.0028 | 0.0770 | 0.1131 | 0.0943 |
| Logistic | 0.5183 | 0.1665 | 0.4173 | 0.6290 | 0.3829 |
| Weighted logistic | **0.5792** | 0.1787 | **0.4373** | **0.8507** | 0.3381 |

The weighted model clears the dummy baseline and improves mean PR-AUC and minority
recall over unweighted logistic regression. Its large fold dispersion is a warning:
results depend materially on which feature groups are held out.

## Threshold and holdout behavior

The following cost sensitivity is calculated only from training out-of-fold
predictions. Costs are relative units, not estimated currency values.

| FN:FP cost | Threshold | Training FN | Training FP | Relative cost |
|---:|---:|---:|---:|---:|
| 2:1 | 0.9645 | 133 | 64 | 330 |
| 5:1 | **0.6700** | 41 | 288 | 493 |
| 10:1 | 0.5284 | 33 | 352 | 682 |

The untouched holdout was evaluated once:

| Threshold | PR-AUC | ROC-AUC | Recall | Precision | F1 | FN | FP | Accuracy |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.5000 | 0.4230 | 0.9142 | 0.8727 | 0.3333 | 0.4824 | 7 | 96 | 0.8614 |
| 0.6700 | 0.4230 | 0.9142 | 0.8545 | 0.4234 | 0.5663 | 8 | 64 | 0.9031 |

Accuracy is included for context only. A majority-only rule would already appear
strong on this target, so PR-AUC and minority errors drive the decision.

## Calibration and reliability slices

The selected model's holdout Brier score is `0.1194`. It is overconfident: the
highest probability bin averages `0.9856`, while its observed minority rate is only
`0.4754`. Its ranking is useful, but its probabilities should not be presented as
calibrated risk without a separately validated calibration stage.

At the selected threshold, rows with exactly one missing feature account for 7 of 8
false negatives and all 64 false positives. The `VOL=f` slice has 48 false positives
and no false negatives; `VOL=t` has 16 false positives and all 8 false negatives.
`KAT=ccc` has 16 false positives and 2 false negatives, while `KAT=ddd` has 48 and 6.
These are reliability slices over anonymized fields, not fairness claims.

## CatBoost decision

The nonlinear candidate was assessed and stopped before execution. The effective
sample contains only 490 independent feature groups, linear CV dispersion is already
high, and the mandatory baseline completes in about three seconds. Adding a new
runtime dependency and up to 20 trials would increase tuning degrees of freedom
without resolving the main uncertainty. A future CatBoost trial is justified only
with the identical grouped folds, a predeclared PR-AUC improvement margin, early
stopping, a 15-minute ceiling, and serialized holdout parity.

## Reproduction

From the repository root, with the private input directory outside Git:

```bash
uv run --locked python -m binary_classification.evaluate \
  --part1 "$SIEMENS_TASK4_INPUT_DIR/Training_part1.csv" \
  --part2 "$SIEMENS_TASK4_INPUT_DIR/Training_part2.csv" \
  --output-dir /tmp/task4-run \
  --seed 42
```

The command writes aggregate `metrics.json` and `selected-model.pkl`. Pickle artifacts
must be loaded only when they were produced by this trusted training run; pickle is
not a safe interchange format for untrusted files. The committed aggregate metrics
contain no source rows.

## Limitations

- Feature names and values are anonymized, so causal, post-outcome, and fairness
  interpretations cannot be established.
- The holdout is one grouped split, and only 55 minority rows appear in it.
- The 5:1 cost ratio is illustrative and must be replaced by an owner-approved loss
  model before deployment.
- Calibration is inadequate for decisions that require probability meaning.
- Drift, temporal ordering, external validation, and live monitoring are not
  available in the assignment data.
