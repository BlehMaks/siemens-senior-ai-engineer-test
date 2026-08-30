"""Group-audited sigmoid calibration and calibration diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.metrics import brier_score_loss, log_loss  # type: ignore[import-untyped]

from binary_classification.modeling import _validate_probabilities

_CLIP_EPSILON = 1e-6


def _logit(probabilities: NDArray[np.float64], epsilon: float) -> NDArray[np.float64]:
    clipped = np.clip(probabilities, epsilon, 1.0 - epsilon)
    return np.log(clipped / (1.0 - clipped))


@dataclass(frozen=True, slots=True)
class SigmoidCalibrator:
    """Serializable Platt-style mapping fitted on grouped OOF probabilities."""

    slope: float
    intercept: float
    clip_epsilon: float = _CLIP_EPSILON

    def __post_init__(self) -> None:
        if (
            not np.isfinite(self.slope)
            or not np.isfinite(self.intercept)
            or not 0.0 < self.clip_epsilon < 0.5
        ):
            raise ValueError("invalid sigmoid calibrator parameters")

    def predict(self, probabilities: NDArray[np.float64]) -> NDArray[np.float64]:
        """Calibrate an equal-shape vector of finite probabilities."""

        scores = np.asarray(probabilities, dtype="float64")
        if scores.ndim != 1 or not len(scores):
            raise ValueError("probabilities must be a non-empty vector")
        if not np.isfinite(scores).all() or ((scores < 0.0) | (scores > 1.0)).any():
            raise ValueError("probabilities must be finite values between 0 and 1")
        linear = self.slope * _logit(scores, self.clip_epsilon) + self.intercept
        calibrated = 1.0 / (1.0 + np.exp(-np.clip(linear, -709.0, 709.0)))
        return np.asarray(calibrated, dtype="float64")

    def to_dict(self) -> dict[str, float | str]:
        """Return the portable manifest representation."""

        return {
            "method": "grouped_oof_sigmoid",
            "slope": self.slope,
            "intercept": self.intercept,
            "clip_epsilon": self.clip_epsilon,
        }


def fit_grouped_sigmoid_calibrator(
    target: pd.Series[bool] | NDArray[np.bool_],
    oof_probabilities: NDArray[np.float64],
    *,
    groups: pd.Series[int] | NDArray[np.int64],
    fold_assignments: NDArray[np.int64],
) -> SigmoidCalibrator:
    """Fit only after proving each duplicate group belongs to one OOF fold."""

    truth, scores = _validate_probabilities(target, oof_probabilities)
    group_values = np.asarray(groups)
    folds = np.asarray(fold_assignments)
    if (
        group_values.ndim != 1
        or folds.ndim != 1
        or len(group_values) != len(truth)
        or len(folds) != len(truth)
    ):
        raise ValueError(
            "target, groups, folds, and probabilities must have equal length"
        )
    if pd.isna(group_values).any() or pd.isna(folds).any():
        raise ValueError("groups and fold assignments cannot be missing")
    group_folds = pd.DataFrame({"group": group_values, "fold": folds})
    if group_folds.groupby("group", dropna=False)["fold"].nunique().max() != 1:
        raise ValueError("duplicate feature groups cannot cross OOF folds")
    if len(np.unique(folds)) < 2:
        raise ValueError("calibration requires at least two OOF folds")
    if len(np.unique(truth)) != 2:
        raise ValueError("calibration requires both target classes")
    model = LogisticRegression(C=np.inf, solver="lbfgs", max_iter=1_000)
    model.fit(_logit(scores, _CLIP_EPSILON).reshape(-1, 1), truth)
    return SigmoidCalibrator(
        slope=float(model.coef_[0, 0]),
        intercept=float(model.intercept_[0]),
    )


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """One deterministic equal-frequency calibration-table row."""

    rows: int
    minimum_probability: float
    maximum_probability: float
    mean_probability: float
    observed_rate: float


@dataclass(frozen=True, slots=True)
class CalibrationMetrics:
    """Proper scores and diagnostic calibration line for a held-out vector."""

    brier_score: float
    log_loss: float
    slope: float
    intercept: float
    table: tuple[CalibrationBin, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe report representation."""

        return asdict(self)


def calibration_metrics(
    target: pd.Series[bool] | NDArray[np.bool_],
    probabilities: NDArray[np.float64],
    *,
    bins: int = 10,
) -> CalibrationMetrics:
    """Calculate held-out diagnostics without reusing the fitted calibrator."""

    if isinstance(bins, bool) or not isinstance(bins, int) or bins < 1:
        raise ValueError("bins must be a positive integer")
    truth, scores = _validate_probabilities(target, probabilities)
    if len(np.unique(truth)) != 2:
        raise ValueError("calibration metrics require both target classes")
    diagnostic = LogisticRegression(C=np.inf, solver="lbfgs", max_iter=1_000)
    diagnostic.fit(_logit(scores, _CLIP_EPSILON).reshape(-1, 1), truth)
    order = np.argsort(scores, kind="stable")
    table = tuple(
        CalibrationBin(
            rows=len(index),
            minimum_probability=float(scores[index].min()),
            maximum_probability=float(scores[index].max()),
            mean_probability=float(scores[index].mean()),
            observed_rate=float(truth[index].mean()),
        )
        for index in np.array_split(order, min(bins, len(scores)))
    )
    return CalibrationMetrics(
        brier_score=float(brier_score_loss(truth, scores)),
        log_loss=float(log_loss(truth, scores, labels=[False, True])),
        slope=float(diagnostic.coef_[0, 0]),
        intercept=float(diagnostic.intercept_[0]),
        table=table,
    )
