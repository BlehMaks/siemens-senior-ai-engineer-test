from __future__ import annotations

import json
import os
import pickle
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import binary_classification.evaluate as evaluate_module
import binary_classification.modeling as modeling_module
from binary_classification.analysis import analyze_training_frame, feature_group_ids
from binary_classification.calibration import SigmoidCalibrator
from binary_classification.decision import EXAMPLE_SCENARIOS, DecisionScenario
from binary_classification.diagnostics import assess_drift
from binary_classification.evaluate import render_comparison_markdown, run_experiment
from binary_classification.modeling import (
    CANDIDATE_NAMES,
    FeatureSchema,
    _validate_grouped_folds,
    build_pipeline,
    choose_threshold,
    cross_validate_candidates,
    grouped_fold_assignments,
    infer_feature_schema,
    metrics_at_threshold,
    prepare_features,
    select_candidate,
    split_train_holdout,
)


def _approximate_floats(value: Any) -> Any:
    if isinstance(value, float):
        return pytest.approx(value, rel=1e-12, abs=1e-15)
    if isinstance(value, dict):
        return {key: _approximate_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_approximate_floats(item) for item in value]
    return value


def _synthetic_tables(groups: int = 60) -> tuple[pd.DataFrame, pd.DataFrame]:
    part1_rows: list[dict[str, Any]] = []
    part2_rows: list[dict[str, Any]] = []
    entity_id = 0
    for group in range(groups):
        target = "n" if group % 4 == 0 else "y"
        for _ in range(2):
            part1_rows.append(
                {
                    "BIB": group % 11,
                    "COD": ("a", "b", "c")[group % 3],
                    "ERG": ("e1", "e2", "e3", "e4", "e5")[group % 5],
                    "FAN": group % 13,
                    "GJAH": ("g1", "g2", "g3")[group % 3],
                    "LUK": group % 17,
                    "MYR": f"m{group % 7}",
                    "NUS": group % 19,
                    "PKD": f"p{group % 5}",
                    "RAS": None if group % 6 == 0 else f"r{group % 3}",
                    "id": entity_id,
                }
            )
            part2_rows.append(
                {
                    "SIS": group % 23,
                    "TOK": "t" if group % 3 == 0 else "f",
                    "UIN": group,
                    "VOL": "risk" if target == "n" else "normal",
                    "WET": group % 5,
                    "KAT": f"k{group % 3}",
                    "XIN": "t" if group % 5 == 0 else "f",
                    "Class": target,
                    "id": entity_id,
                }
            )
            entity_id += 1
    return pd.DataFrame(part1_rows), pd.DataFrame(part2_rows)


def _joined_frame(groups: int = 60) -> pd.DataFrame:
    part1, part2 = _synthetic_tables(groups)
    return part1.merge(part2, on="id", validate="one_to_one")


def test_holdout_is_deterministic_and_keeps_duplicate_vectors_together() -> None:
    frame = _joined_frame()

    first = split_train_holdout(frame, seed=7)
    second = split_train_holdout(frame, seed=7)

    assert set(first.train_groups).isdisjoint(first.holdout_groups)
    assert len(first.train) + len(first.holdout) == len(frame)
    assert first.train["id"].tolist() == second.train["id"].tolist()
    assert first.holdout["id"].tolist() == second.holdout["id"].tolist()
    assert feature_group_ids(frame).nunique() == 60


def test_holdout_rejects_conflicting_targets_within_a_feature_group() -> None:
    frame = _joined_frame()
    duplicate_id = int(frame.iloc[0]["id"])
    frame.loc[frame["id"] == duplicate_id + 1, "Class"] = "y"

    with pytest.raises(ValueError, match="conflicting targets"):
        split_train_holdout(frame)


def test_schema_excludes_quarantined_columns_and_normalizes_types() -> None:
    frame = _joined_frame(20)
    schema = infer_feature_schema(frame, quarantined=("id", "VOL", "UIN"))

    features = prepare_features(frame, schema)

    assert "id" not in schema.columns
    assert "VOL" not in schema.columns
    assert "UIN" not in schema.columns
    assert features["BIB"].dtype == np.dtype("float64")
    assert features.columns.tolist() == list(schema.columns)


def test_modeling_boundaries_reject_invalid_schema_candidate_and_metrics() -> None:
    with pytest.raises(ValueError, match="No modeling features"):
        infer_feature_schema(pd.DataFrame({"id": [1], "Class": ["n"]}))
    with pytest.raises(ValueError, match="Missing model features"):
        prepare_features(
            pd.DataFrame({"numeric": [1]}),
            FeatureSchema(numeric=("numeric",), categorical=("missing",)),
        )
    with pytest.raises(ValueError, match="Unknown candidate"):
        build_pipeline("unknown", FeatureSchema(numeric=("numeric",), categorical=()))
    empty_target: Any = pd.Series([], dtype="bool")
    with pytest.raises(ValueError, match="cannot be empty"):
        choose_threshold(empty_target, np.array([]), false_negative_cost=1)
    with pytest.raises(ValueError, match="both target classes"):
        metrics_at_threshold(pd.Series([True, True]), np.array([0.2, 0.8]), 0.5)


def test_grouped_fold_validation_rejects_each_invalid_boundary() -> None:
    target = pd.Series([False, True])
    groups = pd.Series([0, 1], dtype="int64")
    with pytest.raises(ValueError, match="at least 2"):
        _validate_grouped_folds(target, groups, 1)
    with pytest.raises(ValueError, match="equal length"):
        _validate_grouped_folds(target, groups.iloc[:1], 2)
    missing = groups.astype("float64")
    missing.iloc[0] = np.nan
    with pytest.raises(ValueError, match="missing"):
        _validate_grouped_folds(target, missing, 2)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="distinct feature groups"):
        _validate_grouped_folds(target, groups, 2)

    frame = _joined_frame(20)
    with pytest.raises(ValueError, match="equal length"):
        grouped_fold_assignments(
            frame, feature_group_ids(frame).iloc[:-1], seed=1, folds=2
        )


def test_grouped_fold_assignment_detects_incomplete_splitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompleteSplitter:
        def __init__(self, **_: Any) -> None:
            pass

        def split(self, *_: Any) -> list[tuple[np.ndarray, np.ndarray]]:
            return [(np.arange(1, 20), np.array([0]))]

    frame = _joined_frame(20)
    monkeypatch.setattr(modeling_module, "StratifiedGroupKFold", IncompleteSplitter)

    with pytest.raises(RuntimeError, match="did not assign every row"):
        grouped_fold_assignments(frame, feature_group_ids(frame), seed=1, folds=5)


def test_preprocessing_is_fitted_only_on_training_categories() -> None:
    schema = FeatureSchema(numeric=("numeric",), categorical=("category",))
    training = pd.DataFrame(
        {
            "numeric": [1.0, 2.0, 3.0, 4.0],
            "category": ["a", "b", "a", "b"],
        }
    )
    target = pd.Series([False, True, False, True])
    validation = pd.DataFrame({"numeric": [5.0], "category": ["unseen"]})
    pipeline = build_pipeline("logistic", schema)

    pipeline.fit(training, target)
    preprocessor: Any = pipeline.named_steps["preprocess"]
    encoder: Any = preprocessor.named_transformers_["categorical"].named_steps["encode"]

    assert encoder.categories_[0].tolist() == ["a", "b"]
    assert pipeline.predict_proba(validation).shape == (1, 2)


def test_categorical_preprocessing_handles_empty_folds_and_literal_sentinel() -> None:
    schema = FeatureSchema(numeric=("numeric",), categorical=("category",))
    all_missing = pd.DataFrame({"numeric": range(10), "category": [None] * 10})
    target = pd.Series([False, True] * 5)
    pipeline = build_pipeline("logistic", schema)

    pipeline.fit(prepare_features(all_missing, schema), target)
    unseen = prepare_features(
        pd.DataFrame({"numeric": [5], "category": ["unseen"]}), schema
    )
    assert np.isfinite(pipeline.predict_proba(unseen)).all()

    distinct = pd.DataFrame(
        {
            "numeric": range(8),
            "category": [None] * 4 + ["__MISSING__"] * 4,
        }
    )
    distinct_target = pd.Series([False] * 4 + [True] * 4)
    pipeline.fit(prepare_features(distinct, schema), distinct_target)
    probabilities = pipeline.predict_proba(prepare_features(distinct, schema))[:, 1]
    assert float(probabilities[:4].mean()) < float(probabilities[4:].mean())


def test_all_baselines_run_with_deterministic_grouped_cross_validation() -> None:
    frame = _joined_frame()
    split = split_train_holdout(frame, seed=11)
    leakage = analyze_training_frame(split.train, seed=11).leakage
    schema = infer_feature_schema(split.train, quarantined=leakage.quarantined_columns)

    first, first_probabilities = cross_validate_candidates(
        split.train, split.train_groups, schema, seed=11
    )
    second, second_probabilities = cross_validate_candidates(
        split.train, split.train_groups, schema, seed=11
    )

    assert tuple(candidate.name for candidate in first) == CANDIDATE_NAMES
    assert first == second
    for name in CANDIDATE_NAMES:
        np.testing.assert_array_equal(
            first_probabilities[name], second_probabilities[name]
        )
    assert select_candidate(first) in CANDIDATE_NAMES


def test_cross_validation_rejects_groups_that_split_identical_vectors() -> None:
    frame = _joined_frame(20)
    schema = infer_feature_schema(frame)
    row_groups = pd.Series(range(len(frame)), dtype="int64")

    with pytest.raises(ValueError, match="complete feature groups"):
        cross_validate_candidates(frame, row_groups, schema)


def test_model_selection_rejects_an_incomplete_baseline_set() -> None:
    frame = _joined_frame()
    split = split_train_holdout(frame)
    schema = infer_feature_schema(split.train)
    candidates, _ = cross_validate_candidates(split.train, split.train_groups, schema)

    with pytest.raises(ValueError, match="all declared baseline candidates"):
        select_candidate(candidates[1:])
    with pytest.raises(ValueError, match="all declared baseline candidates"):
        select_candidate((candidates[0], candidates[0], *candidates[1:]))
    with pytest.raises(ValueError, match="finite"):
        select_candidate(
            (replace(candidates[0], mean_pr_auc=float("nan")), *candidates[1:])
        )

    tied = tuple(
        replace(candidate, mean_pr_auc=0.5) for candidate in reversed(candidates)
    )
    assert select_candidate(tied) == CANDIDATE_NAMES[0]


def test_threshold_minimizes_declared_business_cost() -> None:
    target = pd.Series([True, True, False, False])
    probabilities = np.array([0.9, 0.4, 0.6, 0.1], dtype="float64")

    low_fn_cost = choose_threshold(
        target, probabilities, false_negative_cost=1.0, false_positive_cost=1.0
    )
    high_fn_cost = choose_threshold(
        target, probabilities, false_negative_cost=10.0, false_positive_cost=1.0
    )

    assert low_fn_cost.threshold == pytest.approx(0.9)
    assert high_fn_cost.threshold == pytest.approx(0.4)
    assert high_fn_cost.false_negative == 0


def test_threshold_can_choose_all_negative_at_probability_boundary() -> None:
    target = pd.Series([True, False])
    probabilities = np.array([1.0, 1.0], dtype="float64")

    choice = choose_threshold(
        target,
        probabilities,
        false_negative_cost=1.0,
        false_positive_cost=10.0,
    )
    metrics = metrics_at_threshold(target, probabilities, choice.threshold)

    assert choice.threshold == np.nextafter(1.0, np.inf)
    assert choice.total_cost == 1.0
    assert choice.false_negative == 1
    assert choice.false_positive == 0
    assert metrics.recall == 0.0
    with pytest.raises(ValueError, match="supported decision range"):
        metrics_at_threshold(
            target,
            probabilities,
            np.nextafter(choice.threshold, np.inf),
        )

    ordinary_choice = choose_threshold(
        target,
        np.array([0.2, 0.9], dtype="float64"),
        false_negative_cost=1.0,
        false_positive_cost=100.0,
    )
    assert ordinary_choice.threshold == 1.0


@pytest.mark.parametrize(
    ("false_negative_cost", "false_positive_cost"),
    [
        (0.0, 1.0),
        (1.0, -1.0),
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (1.0, float("nan")),
        (1.0, float("inf")),
    ],
)
def test_threshold_rejects_invalid_costs(
    false_negative_cost: float, false_positive_cost: float
) -> None:
    with pytest.raises(ValueError, match="costs must be finite and positive"):
        choose_threshold(
            pd.Series([True, False]),
            np.array([0.8, 0.2]),
            false_negative_cost=false_negative_cost,
            false_positive_cost=false_positive_cost,
        )


@pytest.mark.parametrize(
    "probabilities",
    [
        np.array([0.2]),
        np.array([0.2, float("nan")]),
        np.array([0.2, 1.1]),
        np.array([[0.2, 0.8]]),
    ],
)
def test_threshold_rejects_invalid_probability_vectors(
    probabilities: np.ndarray[Any, Any],
) -> None:
    with pytest.raises(ValueError, match=r"probabilities|vectors"):
        choose_threshold(
            pd.Series([True, False]), probabilities, false_negative_cost=1.0
        )


def test_end_to_end_run_serializes_pipeline_metrics_and_is_reproducible(
    tmp_path: Path,
) -> None:
    part1, part2 = _synthetic_tables()
    part1_path = tmp_path / "part1.csv"
    part2_path = tmp_path / "part2.csv"
    part1.to_csv(part1_path, sep=";", index=False)
    part2.to_csv(part2_path, sep=";", index=False)

    first = run_experiment(part1_path, part2_path, tmp_path / "first", seed=19)
    second = run_experiment(part1_path, part2_path, tmp_path / "second", seed=19)

    assert first.to_dict() == second.to_dict()
    assert first.selected_model in CANDIDATE_NAMES
    assert 0.0 <= first.selected_threshold <= 1.0
    assert len(first.threshold_sensitivity) == 3
    assert {item.dimension for item in first.error_slices} >= {"missing_features"}
    assert (
        sum(
            item.rows
            for item in first.error_slices
            if item.dimension == "missing_features"
        )
        == first.holdout_rows
    )
    metrics_path = tmp_path / "first" / "metrics.json"
    assert json.loads(metrics_path.read_text(encoding="utf-8")) == first.to_dict()
    with (tmp_path / "first" / "selected-model.pkl").open("rb") as model_file:
        restored: Any = pickle.load(model_file)
    assert set(restored) == {"pipeline", "schema"}


def test_extension_run_calibrates_serializes_and_writes_one_source_report(
    tmp_path: Path,
) -> None:
    part1, part2 = _synthetic_tables()
    part1_path = tmp_path / "part1.csv"
    part2_path = tmp_path / "part2.csv"
    part1.to_csv(part1_path, sep=";", index=False)
    part2.to_csv(part2_path, sep=";", index=False)
    output = tmp_path / "extension"

    result = run_experiment(
        part1_path,
        part2_path,
        output,
        seed=23,
        cost_scenarios=tuple(EXAMPLE_SCENARIOS.values()),
    )

    report = json.loads(
        (output / "baseline-vs-extension.json").read_text(encoding="utf-8")
    )
    markdown = (output / "baseline-vs-extension.md").read_text(encoding="utf-8")
    with (output / "selected-model.pkl").open("rb") as model_file:
        artifact: Any = pickle.load(model_file)

    assert report["schema_version"] == "1.1"
    assert report["assignment_baseline"]["selected_model"] == result.selected_model
    assert report["business_extension"]["mode_status"] == "evaluated"
    assert report["business_extension"]["calibration"]["artifact_round_trip_parity"]
    assert len(report["business_extension"]["decision_scenarios"]) == 2
    diagnostics = report["business_extension"]["diagnostics"]
    assert diagnostics["mode_status"] == "evaluated"
    assert diagnostics["status"] in {"ok", "warning"}
    assert diagnostics["reference"]["training_rows"] == result.training_rows
    assert "observed_values" not in json.dumps(diagnostics)
    assert report["metadata"]["row_counts"] == {
        "training": result.training_rows,
        "holdout": result.holdout_rows,
    }
    assert markdown == render_comparison_markdown(report)
    assert "risk" not in json.dumps(report)
    assert set(artifact) == {
        "artifact_schema_version",
        "calibrator",
        "decision_scenarios",
        "diagnostic_reference",
        "pipeline",
        "schema",
        "selected_model",
    }
    assert artifact["artifact_schema_version"] == "3.0"


def test_invalid_diagnostic_schema_blocks_before_final_model_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    part1, part2 = _synthetic_tables()
    part1_path = tmp_path / "part1.csv"
    part2_path = tmp_path / "part2.csv"
    part1.to_csv(part1_path, sep=";", index=False)
    part2.to_csv(part2_path, sep=";", index=False)

    class ForbiddenPipeline:
        def fit(self, *_: Any) -> None:
            raise AssertionError("invalid schema must block before model fit")

        def predict_proba(self, *_: Any) -> np.ndarray:
            raise AssertionError("invalid schema must block before prediction")

    monkeypatch.setattr(
        evaluate_module,
        "assess_drift",
        lambda *_: {
            "status": "invalid_schema",
            "schema": {"missing_required_columns": ["numeric"]},
        },
    )
    monkeypatch.setattr(
        evaluate_module, "build_pipeline", lambda *_args, **_kwargs: ForbiddenPipeline()
    )

    with pytest.raises(ValueError, match="Invalid holdout schema"):
        run_experiment(
            part1_path,
            part2_path,
            tmp_path / "output",
            cost_scenarios=(EXAMPLE_SCENARIOS["balanced-review"],),
        )


def test_diagnostic_warning_does_not_block_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    part1, part2 = _synthetic_tables()
    part1_path = tmp_path / "part1.csv"
    part2_path = tmp_path / "part2.csv"
    part1.to_csv(part1_path, sep=";", index=False)
    part2.to_csv(part2_path, sep=";", index=False)

    def warning_assessment(*args: Any) -> dict[str, Any]:
        report = assess_drift(*args)
        report["status"] = "warning"
        report["warnings"] = [
            {
                "code": "numeric_distribution_shift",
                "column": "BIB",
                "observed": 2.0,
                "threshold": 1.0,
            }
        ]
        return report

    monkeypatch.setattr(evaluate_module, "assess_drift", warning_assessment)

    run_experiment(
        part1_path,
        part2_path,
        tmp_path / "output",
        cost_scenarios=(EXAMPLE_SCENARIOS["balanced-review"],),
    )

    report = json.loads(
        (tmp_path / "output" / "baseline-vs-extension.json").read_text(encoding="utf-8")
    )
    assert report["business_extension"]["diagnostics"]["status"] == "warning"
    assert "numeric_distribution_shift" in (
        tmp_path / "output" / "baseline-vs-extension.md"
    ).read_text(encoding="utf-8")


def test_extension_rejects_duplicate_names_and_wrong_label_mapping() -> None:
    duplicate = EXAMPLE_SCENARIOS["balanced-review"]
    with pytest.raises(ValueError, match="names must be unique"):
        run_experiment(
            "missing-part1",
            "missing-part2",
            "unused",
            cost_scenarios=(duplicate, duplicate),
        )
    reversed_labels = DecisionScenario(
        name="reversed",
        false_positive_cost=1.0,
        false_negative_cost=1.0,
        review_cost=1.0,
        negative_label="n",
        positive_label="y",
    )
    with pytest.raises(ValueError, match="labels must map"):
        run_experiment(
            "missing-part1",
            "missing-part2",
            "unused",
            cost_scenarios=(reversed_labels,),
        )


def test_run_rejects_raw_artifact_parity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    part1, part2 = _synthetic_tables()
    part1_path = tmp_path / "part1.csv"
    part2_path = tmp_path / "part2.csv"
    part1.to_csv(part1_path, sep=";", index=False)
    part2.to_csv(part2_path, sep=";", index=False)
    original_load = pickle.load

    class ChangedPipeline:
        def __init__(self, pipeline: Any) -> None:
            self.pipeline = pipeline

        def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
            probabilities = np.asarray(
                self.pipeline.predict_proba(features), dtype="float64"
            ).copy()
            probabilities[0, 1] = np.nextafter(probabilities[0, 1], 1.0)
            return probabilities

    def changed_load(model_file: Any) -> dict[str, Any]:
        artifact: dict[str, Any] = original_load(model_file)
        artifact["pipeline"] = ChangedPipeline(artifact["pipeline"])
        return artifact

    monkeypatch.setattr(pickle, "load", changed_load)

    with pytest.raises(RuntimeError, match="model predictions differ"):
        run_experiment(part1_path, part2_path, tmp_path / "output")


def test_run_rejects_calibrator_artifact_parity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    part1, part2 = _synthetic_tables()
    part1_path = tmp_path / "part1.csv"
    part2_path = tmp_path / "part2.csv"
    part1.to_csv(part1_path, sep=";", index=False)
    part2.to_csv(part2_path, sep=";", index=False)
    original_predict = SigmoidCalibrator.predict
    calls = 0

    def unstable_predict(
        self: SigmoidCalibrator, probabilities: np.ndarray
    ) -> np.ndarray:
        nonlocal calls
        calls += 1
        result = np.asarray(original_predict(self, probabilities), dtype="float64")
        return result if calls == 1 else np.clip(result + 1e-4, 0.0, 1.0)

    monkeypatch.setattr(SigmoidCalibrator, "predict", unstable_predict)

    with pytest.raises(RuntimeError, match="calibrator predictions differ"):
        run_experiment(
            part1_path,
            part2_path,
            tmp_path / "output",
            cost_scenarios=(EXAMPLE_SCENARIOS["balanced-review"],),
        )


def test_committed_metrics_match_private_data_when_supplied(tmp_path: Path) -> None:
    input_dir_value = os.environ.get("SIEMENS_TASK4_INPUT_DIR")
    if input_dir_value is None:
        pytest.skip("Set SIEMENS_TASK4_INPUT_DIR to reproduce committed metrics")
    input_dir = Path(input_dir_value)

    result = run_experiment(
        input_dir / "Training_part1.csv",
        input_dir / "Training_part2.csv",
        tmp_path / "private-run",
        seed=42,
    )
    metrics_path = Path(__file__).parents[1] / "reports" / "metrics.json"

    committed_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert result.to_dict() == _approximate_floats(committed_metrics)
