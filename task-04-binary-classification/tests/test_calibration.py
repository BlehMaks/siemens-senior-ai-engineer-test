from __future__ import annotations

import pickle

import numpy as np
import pandas as pd
import pytest

from binary_classification.calibration import (
    SigmoidCalibrator,
    calibration_metrics,
    fit_grouped_sigmoid_calibrator,
)


def _calibration_case() -> tuple[
    pd.Series[bool], np.ndarray, pd.Series[int], np.ndarray
]:
    target = pd.Series([False, False, True, True] * 4)
    probabilities = np.array(
        [0.1, 0.2, 0.7, 0.8, 0.2, 0.3, 0.6, 0.9] * 2,
        dtype="float64",
    )
    groups = pd.Series(np.repeat(np.arange(8), 2), dtype="int64")
    folds = np.repeat(np.arange(4), 4).astype("int64")
    return target, probabilities, groups, folds


def test_grouped_sigmoid_is_deterministic_bounded_and_serializable() -> None:
    target, probabilities, groups, folds = _calibration_case()

    first = fit_grouped_sigmoid_calibrator(
        target, probabilities, groups=groups, fold_assignments=folds
    )
    second = fit_grouped_sigmoid_calibrator(
        target, probabilities, groups=groups, fold_assignments=folds
    )
    restored = pickle.loads(pickle.dumps(first))
    calibrated = first.predict(np.array([0.0, 0.5, 1.0]))

    assert first == second
    assert first.to_dict()["method"] == "grouped_oof_sigmoid"
    assert np.all((calibrated >= 0.0) & (calibrated <= 1.0))
    np.testing.assert_array_equal(
        calibrated, restored.predict(np.array([0.0, 0.5, 1.0]))
    )


def test_grouped_calibration_rejects_fold_leakage_and_invalid_inputs() -> None:
    target, probabilities, groups, folds = _calibration_case()
    crossing = folds.copy()
    crossing[1] = 1
    with pytest.raises(ValueError, match="cannot cross"):
        fit_grouped_sigmoid_calibrator(
            target, probabilities, groups=groups, fold_assignments=crossing
        )
    with pytest.raises(ValueError, match="at least two"):
        fit_grouped_sigmoid_calibrator(
            target,
            probabilities,
            groups=groups,
            fold_assignments=np.zeros(len(target), dtype="int64"),
        )
    with pytest.raises(ValueError, match="both target classes"):
        fit_grouped_sigmoid_calibrator(
            pd.Series([False] * len(target)),
            probabilities,
            groups=groups,
            fold_assignments=folds,
        )
    with pytest.raises(ValueError, match="equal length"):
        fit_grouped_sigmoid_calibrator(
            target,
            probabilities,
            groups=groups.iloc[:-1],
            fold_assignments=folds,
        )
    missing_groups = groups.astype("float64")
    missing_groups.iloc[0] = np.nan
    with pytest.raises(ValueError, match="cannot be missing"):
        fit_grouped_sigmoid_calibrator(
            target,
            probabilities,
            groups=missing_groups,  # type: ignore[arg-type]
            fold_assignments=folds,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slope": float("nan"), "intercept": 0.0},
        {"slope": 1.0, "intercept": float("inf")},
        {"slope": 1.0, "intercept": 0.0, "clip_epsilon": 0.5},
    ],
)
def test_calibrator_rejects_invalid_parameters(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="invalid sigmoid"):
        SigmoidCalibrator(**kwargs)


@pytest.mark.parametrize(
    "probabilities",
    [np.array([]), np.array([[0.5]]), np.array([float("nan")]), np.array([1.1])],
)
def test_calibrator_rejects_invalid_prediction_vectors(
    probabilities: np.ndarray,
) -> None:
    with pytest.raises(ValueError, match="probabilities"):
        SigmoidCalibrator(1.0, 0.0).predict(probabilities)


def test_calibration_metrics_include_proper_scores_line_and_table() -> None:
    target, probabilities, _, _ = _calibration_case()

    metrics = calibration_metrics(target, probabilities, bins=5)

    assert 0.0 <= metrics.brier_score <= 1.0
    assert metrics.log_loss > 0.0
    assert np.isfinite(metrics.slope)
    assert np.isfinite(metrics.intercept)
    assert len(metrics.table) == 5
    assert sum(item.rows for item in metrics.table) == len(target)
    assert len(metrics.to_dict()["table"]) == 5


@pytest.mark.parametrize("bins", [0, -1, True, 1.5])
def test_calibration_metrics_reject_invalid_bins(bins: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        calibration_metrics(
            pd.Series([False, True]),
            np.array([0.1, 0.9]),
            bins=bins,  # type: ignore[arg-type]
        )


def test_calibration_metrics_require_both_classes() -> None:
    with pytest.raises(ValueError, match="both target classes"):
        calibration_metrics(pd.Series([False, False]), np.array([0.1, 0.2]))
