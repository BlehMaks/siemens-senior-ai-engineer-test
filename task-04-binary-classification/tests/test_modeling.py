from __future__ import annotations

import json
import os
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from binary_classification.analysis import analyze_training_frame, feature_group_ids
from binary_classification.evaluate import run_experiment
from binary_classification.modeling import (
    CANDIDATE_NAMES,
    FeatureSchema,
    build_pipeline,
    choose_threshold,
    cross_validate_candidates,
    infer_feature_schema,
    prepare_features,
    select_candidate,
    split_train_holdout,
)


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


def test_model_selection_rejects_an_incomplete_baseline_set() -> None:
    frame = _joined_frame()
    split = split_train_holdout(frame)
    schema = infer_feature_schema(split.train)
    candidates, _ = cross_validate_candidates(split.train, split.train_groups, schema)

    with pytest.raises(ValueError, match="all declared baseline candidates"):
        select_candidate(candidates[1:])


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


@pytest.mark.parametrize(
    ("false_negative_cost", "false_positive_cost"), [(0.0, 1.0), (1.0, -1.0)]
)
def test_threshold_rejects_non_positive_costs(
    false_negative_cost: float, false_positive_cost: float
) -> None:
    with pytest.raises(ValueError, match="costs must be positive"):
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

    assert json.loads(metrics_path.read_text(encoding="utf-8")) == result.to_dict()
