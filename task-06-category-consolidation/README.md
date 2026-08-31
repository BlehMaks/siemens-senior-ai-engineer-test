# Task 6: Functions for categorical attributes

This package groups categories with insufficient training support into one
collision-safe fallback label. The assignment helper remains dependency-free; the
business extension is opt-in and adds a pandas/scikit-learn integration, a minimum
count rule, exact reviewed aliases, diagnostics, and a safe mapping artifact.

## Installation profiles

From the repository root, the locked development environment includes all test
dependencies:

```bash
uv sync --locked
```

The Task 6 package can also be installed by itself without pandas or scikit-learn:

```bash
uv venv .venv-task6-base --python 3.12
uv pip install --python .venv-task6-base/bin/python ./task-06-category-consolidation
.venv-task6-base/bin/python -c "import category_consolidation"
```

On Windows/WSL, use `.venv-task6-base/Scripts/python.exe` instead of the `bin`
path. Install the opt-in adapter with the `sklearn` extra:

```bash
uv venv .venv-task6-extra --python 3.12
uv pip install --python .venv-task6-extra/bin/python './task-06-category-consolidation[sklearn]'
```

## Assignment baseline

The assignment wording says "less frequent than the threshold", so categories at
the exact percentage boundary stay untouched. Default behavior is percentage-only
and matches the original helper contract:

```python
from category_consolidation import consolidate_rare_categories

result = consolidate_rare_categories(
    ["common", "common", "rare"],
    threshold_percent=40.0,
)
assert result == ["common", "common", "__RARE__"]
```

`RareCategoryConsolidator` is the reusable train/inference object. Fit it once on
training data and reuse the frozen mapping for validation, test, and inference.
Calling the one-shot helper separately on each split would relearn frequencies and
violate the leakage boundary.

The baseline contract is unchanged:

- input order and output length never change;
- thresholds accept finite real numbers from `0` to `100` inclusive;
- the denominator includes missing sentinels;
- exact percentage boundaries are retained;
- unhashable values fail with their input index;
- fallback labels are made collision-safe at fit time;
- unseen inference categories map to the fallback and appear in diagnostics;
- importing `category_consolidation` requires no optional dependency.

## Business extension

### Dual support rule

`min_count` is optional and disabled by default. When enabled, a category must pass
both the percentage and count boundaries; equality at either boundary is retained.
Booleans, negative values, and non-integers are rejected.

```python
from category_consolidation import consolidate_rare_categories

result = consolidate_rare_categories(
    ["minor", "minor", "major", "major", "major", "major"],
    threshold_percent=20.0,
    min_count=3,
)
assert result[:2] == ["__RARE__", "__RARE__"]
```

### Multi-column sklearn transformer

The optional adapter lives in a separate module, so it cannot add pandas or
scikit-learn to the lightweight baseline import path.

```python
import pandas as pd
from sklearn.pipeline import Pipeline

from category_consolidation.sklearn import CategoryConsolidationTransformer

training = pd.DataFrame(
    {
        "region": ["north", "north", "south", None],
        "channel": ["web", "web", "store", "partner"],
        "value": [1, 2, 3, 4],
    }
)
pipeline = Pipeline(
    [
        (
            "categories",
            CategoryConsolidationTransformer(
                columns=("region", "channel"),
                threshold_percent=25.0,
                min_count=2,
            ),
        )
    ]
).fit(training)
transformed = pipeline.transform(training)
```

The transformer preserves the index, column order, non-selected columns, and
feature names. It rejects ndarray input, duplicate column names, and missing,
extra, or reordered inference columns because its supported contract is a complete
DataFrame schema. These generic ndarray estimator checks do not apply; clone,
parameters, pipeline, pandas output, feature names, and fitted-state behavior are
covered directly.

`transform_with_diagnostics` returns per-column unseen and fallback counts/rates
plus the retained-category count. Transform never changes the training-fitted
mapping.

### Reviewed alias normalization

An opt-in per-column map can consolidate known spelling variants before training
frequencies are counted:

```python
transformer = CategoryConsolidationTransformer(
    columns=("region",),
    threshold_percent=25.0,
    alias_maps={"region": {"nroth": "north"}},
).fit(training)
```

Only exact declared aliases are changed. There is no fuzzy matching, case folding,
or target-based inference: for example, `"North"` remains an unseen category unless
it is declared separately. Canonical targets must already occur in that selected
training column. Alias maps are flat; cycles, chains, reserved fallback/missing
labels, unknown selected columns, and unknown canonical targets fail during `fit`.
The validated map is copied at fit time, so later caller mutations cannot change
inference behavior. Omitting `alias_maps`, passing `{}`, or using only empty
per-column maps is the exact percentage/count behavior above.

### Safe mapping artifact

Portable mappings use strict JSON, never pickle:

```python
from category_consolidation.artifact import (
    dump_mapping_artifact,
    load_mapping_artifact,
)

artifact_json = dump_mapping_artifact(pipeline.named_steps["categories"])
restored = load_mapping_artifact(artifact_json)
```

The artifact records schema version, thresholds, full feature schema, retained and
observed categories, fallback labels, and a SHA-256 fingerprint. Default and empty
alias configurations preserve the existing schema-v1 bytes. Enabled aliases use
schema v2, which adds the sorted reviewed map; the loader supports both versions.
The scalar codec supports only `None`, booleans, integers, floats (including
explicit NaN/infinity tags), strings, and the package missing sentinel. Unsupported
types, duplicate JSON keys, corrupt fingerprints, invalid mappings, and unknown
schema versions fail closed. Loading parses data only and never executes code.

## Comparison report

Generate both committed report formats from one sanitized evaluation object:

```bash
PYTHONPATH=task-06-category-consolidation/src \
  uv run --frozen python -m category_consolidation.evaluation \
  --output-dir task-06-category-consolidation/reports
```

Outputs:

- `reports/baseline-vs-extension.json` for the versioned machine contract;
- `reports/baseline-vs-extension.md` generated directly from that JSON object.

The report measures percent-only equivalence, per-column category/fallback/unseen
statistics, `% + min_count` mapping differences, sklearn integration checks, safe
artifact parity, reviewed-alias evidence, and a bounded runtime/memory
microbenchmark. The alias fixture proves that a declared spelling variant contributes
to its canonical training frequency while an undeclared case variant remains unseen.
Extension-only measures are not described as an improvement to the assignment
baseline.

## Verification

```bash
uv run pytest task-06-category-consolidation/tests
uv run ruff check task-06-category-consolidation
uv run mypy task-06-category-consolidation/src task-06-category-consolidation/tests
```

## Interpretation and limitations

One-hot encoding gives each distinct category its own coefficient. Consolidating
levels with weak training support reduces fragile sparse columns, but it does not
prove that grouped categories are semantically equivalent. The transformer learns
frequencies only from `fit` data and performs no target encoding, fuzzy matching,
case inference, or alias inference.

The committed fixture report is deterministic except for explicitly labeled local
runtime/memory measurements. It is engineering evidence, not a universal
performance promise. Real threshold and minimum-count choices remain owner policy.
Reviewed explicit alias normalization remains disabled by default.

Leakage-safe target encoding with smoothing and cross-fitting is an alternative for
large categorical spaces where many levels still matter. Models with native
categorical handling, such as CatBoost, can also reduce manual grouping, but their
validation split must remain equally leakage-safe.
