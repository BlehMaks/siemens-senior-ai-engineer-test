# Tasks 4–6 requirements traceability

This matrix keeps assignment evidence distinct from opt-in business extensions.
The original Task 4 and Task 5 source tables are private and excluded from Git.
Commands requiring them use `SIEMENS_TASK4_INPUT_DIR` or `SIEMENS_FUSE_CSV`.
Recorded results describe the supplied reference data only; they are not Siemens
production truth.

| Requirement | Assignment baseline evidence | Command or test | Checked-in report | Business extension |
|---|---|---|---|---|
| Task 4: join both training tables by `id` | `binary_classification.data.load_and_join` validates delimiter, schema, duplicates, and one-to-one cardinality | `uv run pytest -q task-04-binary-classification/tests/test_data.py` | `task-04-binary-classification/reports/model-card.md` | No change; extensions consume the validated join. |
| Task 4: describe preprocessing | Fold-fitted numeric and categorical preprocessing is built in `binary_classification.modeling` | `uv run pytest -q task-04-binary-classification/tests/test_modeling.py` | Model card, preprocessing section | Calibration is fitted after base-model selection. |
| Task 4: develop and evaluate a binary classifier | Grouped CV compares dummy and logistic baselines, then evaluates once on an untouched grouped holdout | `uv run python -m binary_classification.evaluate --part1 "$SIEMENS_TASK4_INPUT_DIR/Training_part1.csv" --part2 "$SIEMENS_TASK4_INPUT_DIR/Training_part2.csv" --output-dir /tmp/task4-run` | `task-04-binary-classification/reports/metrics.json` and model card | Opt-in calibrated probabilities and expected-cost decisions remain separately reported. |
| Task 5: analyze attributes, difficulties, and remedies | Reproducible catalog profile documents missing descriptions, duplicates, and unit-bearing fields | `SIEMENS_FUSE_CSV=/path/Fuse.csv uv run pytest -q task-05-material-similarity/tests` | `task-05-material-similarity/reports/data-analysis.md` | Safety evaluation adds conflict and parser coverage without recasting it as certification. |
| Task 5: return five description-based alternatives | Deterministic word/character TF-IDF baseline excludes self and preserves identifiers and technical tokens | `uv run material-similarity "$SIEMENS_FUSE_CSV" --mode text --part-id PART_ID` | `task-05-material-similarity/reports/retrieval-evaluation.md` | Strict hybrid and structured-only modes are explicit version-2 opt-ins. |
| Task 5: plan remaining-attribute use | The hybrid design parses a bounded set of supported technical fields and explains missingness | `uv run pytest -q task-05-material-similarity/tests/test_hybrid.py` | `task-05-material-similarity/reports/hybrid-extension-design.md` | Hard compatibility gates cannot be overridden by text similarity. |
| Task 6: consolidate categories below a percentage threshold | `consolidate_rare_categories` and `RareCategoryConsolidator` preserve order and learn only from training values | `uv run pytest -q task-06-category-consolidation/tests` | Task 6 README examples and contract | Optional `min_count` adds a second support rule while the default remains identical. |
| Task 6: explain benefit for logistic regression | Task 6 README explains sparse unstable coefficients and the leakage boundary | Manual documentation review | Task 6 README | Multi-column sklearn adapter is optional and keeps the standalone module dependency-free. |
| Task 6: discuss alternatives | Task 6 README covers cross-fitted target encoding and native categorical handling | Manual documentation review | Task 6 README | Safe JSON mappings and explicit aliases support integration; no target inference is added. |

The [reviewer guide](reviewer-guide.md) provides the full repository evidence map.
Private-data acceptance and any production interpretation remain owner-owned.
