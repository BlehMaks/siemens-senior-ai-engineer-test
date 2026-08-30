# Task 6: Functions for categorical attributes

## Goal

This package groups categories whose training frequency is strictly less than a
percentage threshold into one collision-safe fallback label.

The assignment wording says "less frequent than the threshold", so categories at
the exact boundary stay untouched. The threshold is learned on training data once
and reused for validation, test, and inference so the preprocessing cannot leak
future label information back into model fitting.

## Public contract

- Input order and output length never change.
- The threshold accepts finite real numbers from `0` to `100` inclusive.
- Boolean thresholds are rejected explicitly so `True` and `False` cannot sneak in
  as `1` and `0`.
- The denominator includes every validated value, including an explicit missing
  sentinel such as `None` or `""`.
- Empty input is valid and returns an empty output.
- `0` keeps every seen category.
- `100` keeps only categories that occupy the full training set.
- Unhashable values fail with an index-aware `TypeError`.
- The fallback label is made unique at fit time if it would collide with a real
  category.
- Unseen inference categories are mapped to the fallback label and reported through
  transform diagnostics.

## API shape

`RareCategoryConsolidator` is the reusable train/inference object.

- `fit(values)` records the observed and retained categories from training data.
- `transform(values)` applies the frozen mapping.
- `transform_with_diagnostics(values)` also reports unseen indexes and values.
- `fit_transform(values)` is a convenience for one-shot training preprocessing.
- `consolidate_rare_categories(values, threshold_percent, ...)` is a thin helper for
  the assignment's standalone-function framing.

This is a fitted deterministic preprocessing transform, not a predictive ML model.
Call `fit` once on the training split and reuse the same object for validation, test,
and inference; calling the one-shot helper separately on each split would relearn
frequencies and violate the leakage boundary. The assignment does not require a
serialized Task 6 artifact, but a production pipeline must persist these fitted
categories together with the downstream model rather than fitting them per request.

## Why this helps logistic regression

One-hot encoding turns every distinct category into its own coefficient. Very rare
levels create sparse columns with weak support, so the fitted coefficients can swing
hard because of noise instead of stable signal. Grouping rare levels reduces the
number of fragile columns, makes the design matrix denser, and lowers the chance
that one accidental category becomes a brittle proxy for the target.

This is still a training-time decision. The threshold and retained-category set must
come only from the training split, then stay frozen for every later split.

## Alternative approaches

Leakage-safe target encoding is the most relevant alternative when the categorical
space is large and many levels still matter. The safe version uses smoothing plus
cross-fitting so each training row only sees target statistics from other folds.
Without that discipline, target encoding leaks label information directly into the
features.

CatBoost is a strong example of an algorithm with native categorical handling. Its
ordered target statistics reduce the need for manual rare-category grouping, but the
validation split still has to stay honest because incorrect evaluation can leak just
as badly there.
