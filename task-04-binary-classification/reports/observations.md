# Task 4 observations, results, and reasoning

Most of the work in this task went into the join and the split, not the model.
This note explains why. Full evidence is in [`model-card.md`](model-card.md),
[`data-analysis.md`](data-analysis.md), and [`metrics.json`](metrics.json).

## What the problem turned out to be

The join is not the routine part. Both supplied files hold 4,070 rows but only 3,700
unique `id` values: each contains 370 exact duplicate rows, and neither has IDs the
other lacks. A naive `merge` on `id` produces 4,475 rows, inventing observations by
pairing duplicates with duplicates. The loader therefore removes exact source rows,
rejects conflicting IDs, and asserts a one-to-one join before anything is modelled.

Two properties of the cleaned table then shape every later decision.

The target is imbalanced: 276 minority (`n`) against 3,424 majority (`y`), about
7.5%. Accuracy is useless at that ratio, since predicting `y` for everything scores
92.5%.

The rows are also not independent. The 3,700 entities collapse to only 490 distinct
complete feature vectors. 214 of those vectors repeat, covering 3,424 rows, and the
largest group holds 16 entities. A random split by `id` would put exact feature
copies on both sides of the holdout and report a score that is partly memorisation.

Missingness is concentrated rather than spread evenly. `RAS` is null in 2,145 rows,
while the next largest gaps are an order of magnitude smaller (`FAN` and `NUS` at
100 each).

## What was done, and why

Splitting groups by the full feature vector rather than by `id`, in both the holdout
and cross-validation, follows directly from the repeated vectors above. Unique
identifiers do not make a split safe when the features underneath them are identical.

Preprocessing is fitted inside training folds only: median imputation and scaling for
numeric columns, most-frequent imputation and one-hot encoding for categorical ones,
each with an explicit missingness indicator. `RAS` being 58% null is itself a signal,
so it is encoded rather than dropped or quietly filled.

Candidates are ranked on PR-AUC against a stratified dummy. At a 7.5% positive rate
PR-AUC reflects minority performance in a way that ROC-AUC and accuracy do not.
`id` is quarantined by construction and can never reach the feature matrix.

## Results

Five-fold `StratifiedGroupKFold` on the training partition:

| Candidate | Mean PR-AUC | Fold SD | OOF recall @0.5 |
|---|---:|---:|---:|
| Stratified dummy | 0.0773 | 0.0028 | 0.1131 |
| Logistic | 0.5161 | 0.1658 | 0.6290 |
| Weighted logistic | **0.5626** | 0.1885 | **0.8507** |

Class-weighted logistic regression was selected. On the untouched holdout it reaches
PR-AUC `0.4230` against a minority base rate of roughly `0.0746`, so about 5.7 times
the base rate. That is a real signal rather than a strong one.

## Caveats

The fold dispersion is the honest headline. An SD of `0.1885` against a mean of
`0.5626` means the score depends materially on which feature groups are held out.
That follows from having only 490 distinct vectors, and no amount of model tuning
removes it.

The operating threshold `0.671291` comes from an illustrative 5:1 false-negative to
false-positive cost ratio. The features are anonymised and no business cost was
supplied, so that ratio demonstrates the mechanism without claiming anything about
Siemens operations. The threshold should be re-chosen against a real loss function
before any use.
