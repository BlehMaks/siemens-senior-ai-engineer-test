# Task 6: Functions for categorical attributes

## Assignment baseline

Implement a function with two inputs:

1. a list of categorical values;
2. a threshold percentage from 0 to 100.

Return a list in which categories occurring less frequently than the threshold are replaced by a generic category. Explain how this helps logistic regression for a binary outcome such as delayed/not delayed. Also describe another method for high-cardinality categorical data and an algorithm that handles categorical attributes without requiring the same preprocessing.

## Recommended stack and design

Use a small typed Python function backed by `collections.Counter`. No data-frame dependency is required for the core behavior. A thin pandas example may demonstrate pipeline integration without coupling the function to pandas.

The contract must define:

- whether the comparison is strictly less than the threshold, matching the assignment wording;
- how missing values are represented and counted;
- behavior for empty input and thresholds at 0 and 100;
- validation for out-of-range thresholds and unhashable values;
- how to avoid a collision between the generic label and a real category;
- preservation of input order and length.

## Modeling explanation

Grouping rare categories reduces the number of one-hot columns, stabilizes coefficients estimated from very small groups, and lowers the chance that a rare level becomes a brittle proxy for the target. The threshold must be fit on training data and reused unchanged for validation, test, and inference data.

For an alternative, compare leakage-safe target encoding with smoothing and cross-fitting. CatBoost is the recommended example of an algorithm with native categorical handling; its ordered target statistics reduce, but do not eliminate, the need for correct validation and leakage controls.

## Acceptance checks

- The function returns the expected values for exact-boundary frequencies.
- Input order and length do not change.
- Empty input and 0/100 thresholds have documented results.
- Invalid thresholds and unsupported values fail with useful messages.
- Training-derived category mappings are reusable on unseen data.
- Tests cover generic-label collision, missing values, and unseen inference categories.
