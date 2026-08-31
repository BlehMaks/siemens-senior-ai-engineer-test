# Task 4 baseline versus business extension

## Assignment baseline

Selected model: `logistic`. The assignment baseline retains raw model probabilities and its predeclared threshold analysis.

| Measure | Raw baseline | Calibrated extension | Delta |
|---|---:|---:|---:|
| Brier score | 0.302721 | 0.169054 | -0.133667 |
| Log loss | 0.885272 | 0.505007 | -0.380265 |
| Calibration slope | -1.481297 | 3.575524 | n/a |
| Calibration intercept | -3.105428 | 3.786420 | n/a |

## Business extension

The extension fits a sigmoid only on grouped out-of-fold training probabilities. The untouched holdout is used for this final comparison.

| Scenario | FP | FN | Review | Auto coverage | Review rate | Auto error | Expected cost | Realized cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| balanced-review | 1.000 | 1.000 | 0.300 | 1.000000 | 0.000000 | 0.250000 | 0.190287 | 0.250000 |
| miss-averse-review | 1.000 | 8.000 | 0.750 | 0.333333 | 0.666667 | 0.250000 | 0.730817 | 0.583333 |

## Training-fitted diagnostics

Status: `ok`. Thresholds are review policy, not universal drift tests.

No diagnostic policy warnings were raised.

## Optional active-review queue

Disabled; no local review queue path was requested.

## Limitations

- Class semantics and real business costs are not provided.
- Bundled scenarios are examples and require owner confirmation.
- The holdout is evaluated only after model, calibration, and policy setup.
- The small minority class makes calibration and cost estimates uncertain.
- Review queue priorities are human-labeling aids, not approval decisions.
