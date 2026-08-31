from __future__ import annotations

import json
import pickle

import numpy as np
import pandas as pd
import pytest

from binary_classification.diagnostics import (
    DiagnosticPolicy,
    assess_drift,
    fit_diagnostic_reference,
)
from binary_classification.modeling import FeatureSchema


def _diagnostic_case() -> tuple[pd.DataFrame, FeatureSchema]:
    frame = pd.DataFrame(
        {
            "numeric": [0.0, 1.0, 2.0, 3.0, 4.0],
            "constant": [1.0] * 5,
            "all_missing": [None] * 5,
            "category": ["a", "b", "a", None, "b"],
        }
    )
    schema = FeatureSchema(
        numeric=("numeric", "constant", "all_missing"),
        categorical=("category",),
    )
    return frame, schema


def test_identical_frame_is_ok_and_reference_is_not_mutated() -> None:
    frame, schema = _diagnostic_case()
    reference = fit_diagnostic_reference(frame, schema)
    before = pickle.dumps(reference)

    report = assess_drift(reference, frame.copy())

    assert report["status"] == "ok"
    assert report["warnings"] == []
    assert report["reference"] == {
        "training_rows": 5,
        "fitted_on": "training_features_only",
    }
    assert report["numeric_distribution"][2]["metric_status"] == (
        "reference_no_observations"
    )
    assert pickle.dumps(reference) == before
    assert pickle.loads(before) == reference
    assert '"a"' not in json.dumps(report)


def test_shifted_frame_emits_named_aggregate_warnings() -> None:
    frame, schema = _diagnostic_case()
    policy = DiagnosticPolicy(
        missing_rate_delta_warning=0.10,
        numeric_quantile_shift_iqr_warning=0.50,
        unseen_category_rate_warning=0.10,
    )
    reference = fit_diagnostic_reference(frame, schema, policy=policy)
    shifted = pd.DataFrame(
        {
            "numeric": [None, 11.0, 12.0, 13.0, 14.0],
            "constant": [2.0] * 5,
            "all_missing": [None] * 5,
            "category": ["unseen", None, "unseen", None, "unseen"],
        }
    )

    report = assess_drift(reference, shifted)

    assert report["status"] == "warning"
    assert {item["code"] for item in report["warnings"]} == {
        "constant_numeric_changed",
        "missingness_shift",
        "numeric_distribution_shift",
        "unseen_category_rate",
    }
    assert {item["column"] for item in report["warnings"]} >= {
        "numeric",
        "constant",
        "category",
    }
    assert report["categorical_unseen"][0]["unseen_count"] == 3
    assert report["numeric_distribution"][1]["shift_iqr"] is None


def test_nan_heavy_input_has_explicit_no_observation_metrics() -> None:
    frame, schema = _diagnostic_case()
    reference = fit_diagnostic_reference(
        frame,
        schema,
        policy=DiagnosticPolicy(missing_rate_delta_warning=0.0),
    )
    current = frame.copy()
    current["numeric"] = np.nan
    current["category"] = None

    report = assess_drift(reference, current)

    numeric = report["numeric_distribution"][0]
    categorical = report["categorical_unseen"][0]
    assert numeric["metric_status"] == "no_observations"
    assert numeric["shift_iqr"] is None
    assert categorical["observed_rows"] == 0
    assert categorical["unseen_rate"] is None
    assert not categorical["warning"]


def test_schema_invalidity_is_distinct_from_non_blocking_extra_column() -> None:
    frame, schema = _diagnostic_case()
    reference = fit_diagnostic_reference(frame, schema)

    missing = assess_drift(reference, frame.drop(columns=["numeric"]))
    duplicate_frame = pd.concat([frame, frame[["numeric"]]], axis=1)
    duplicate_frame.columns = [*frame.columns, "numeric"]
    duplicate = assess_drift(reference, duplicate_frame)
    extra = assess_drift(reference, frame.assign(extra_feature=1))

    assert missing["status"] == "invalid_schema"
    assert missing["schema"]["missing_required_columns"] == ["numeric"]
    assert missing["missingness"] == []
    assert duplicate["status"] == "invalid_schema"
    assert duplicate["schema"]["duplicate_columns"] == ["numeric"]
    assert extra["status"] == "warning"
    assert extra["warnings"] == [
        {
            "code": "schema_unexpected_column",
            "column": "extra_feature",
            "observed": "present",
            "threshold": "absent",
        }
    ]


def test_reference_fit_rejects_invalid_training_schema() -> None:
    frame, schema = _diagnostic_case()
    with pytest.raises(ValueError, match="Invalid training schema"):
        fit_diagnostic_reference(frame.drop(columns=["numeric"]), schema)

    duplicate = pd.concat([frame, frame[["numeric"]]], axis=1)
    duplicate.columns = [*frame.columns, "numeric"]
    with pytest.raises(ValueError, match="Invalid training schema"):
        fit_diagnostic_reference(duplicate, schema)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"missing_rate_delta_warning": -0.1},
        {"missing_rate_delta_warning": 1.1},
        {"unseen_category_rate_warning": float("nan")},
        {"unseen_category_rate_warning": True},
    ],
)
def test_policy_rejects_invalid_rate_thresholds(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="rate warning thresholds"):
        DiagnosticPolicy(**kwargs)


@pytest.mark.parametrize("threshold", [-0.1, float("inf"), True])
def test_policy_rejects_invalid_numeric_threshold(threshold: float) -> None:
    with pytest.raises(ValueError, match="numeric shift threshold"):
        DiagnosticPolicy(numeric_quantile_shift_iqr_warning=threshold)
