# ADR 0008: Calibrate Task 4 decisions after grouped model selection

## Context

The assignment baseline produces probabilities and threshold metrics, but an
operational choice also depends on explicit false-positive, false-negative, and
manual-review costs. Duplicate feature groups make default calibration splits
unsafe.

## Decision

After selecting one base-model configuration with grouped CV, create grouped
out-of-fold probabilities for that configuration and fit a model-agnostic sigmoid
calibrator. Apply a typed expected-cost policy outside the unchanged training
pipeline. Fit neither calibration nor policy on the untouched holdout. See the
[decision-layer diagram](../diagrams/task4-calibrated-decision-layer.mmd).

## Rejected alternatives

- Default calibration CV can split duplicate feature groups.
- Isotonic calibration is too flexible for the small imbalanced sample without
  training-only evidence that it is stable.
- A CatBoost-specific threshold API would couple the decision contract to one model.

## Consequences

The serialized extension records the selected model, calibrator, policy, schema,
and package versions. Example costs are scenarios, not Siemens business truth.
Baseline metrics and extension decisions remain separate in the report.
