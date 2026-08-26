"""Leakage-safe baselines and imbalance-aware evaluation primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.compose import ColumnTransformer  # type: ignore[import-untyped]
from sklearn.dummy import DummyClassifier  # type: ignore[import-untyped]
from sklearn.impute import SimpleImputer  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.metrics import (  # type: ignore[import-untyped]
    accuracy_score,
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedGroupKFold  # type: ignore[import-untyped]
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
from sklearn.preprocessing import (  # type: ignore[import-untyped]
    OneHotEncoder,
    StandardScaler,
)

from binary_classification.analysis import feature_group_ids

TARGET_COLUMN = "Class"
ID_COLUMN = "id"
MINORITY_LABEL = "n"
CANDIDATE_NAMES = ("dummy", "logistic", "weighted_logistic")


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    numeric: tuple[str, ...]
    categorical: tuple[str, ...]

    @property
    def columns(self) -> tuple[str, ...]:
        return self.numeric + self.categorical


@dataclass(frozen=True, slots=True)
class DataSplit:
    train: pd.DataFrame
    holdout: pd.DataFrame
    train_groups: pd.Series[int]
    holdout_groups: pd.Series[int]


@dataclass(frozen=True, slots=True)
class BinaryMetrics:
    pr_auc: float
    roc_auc: float
    precision: float
    recall: float
    f1: float
    accuracy: float
    brier: float
    true_negative: int
    false_positive: int
    false_negative: int
    true_positive: int


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    name: str
    fold_pr_auc: tuple[float, ...]
    mean_pr_auc: float
    std_pr_auc: float
    out_of_fold_at_0_5: BinaryMetrics


@dataclass(frozen=True, slots=True)
class ThresholdChoice:
    false_negative_cost: float
    false_positive_cost: float
    threshold: float
    total_cost: float
    false_negative: int
    false_positive: int


def _is_numeric(series: pd.Series[Any]) -> bool:
    observed = series.dropna()
    return bool(
        not observed.empty and pd.to_numeric(observed, errors="coerce").notna().all()
    )


def infer_feature_schema(
    frame: pd.DataFrame, *, quarantined: tuple[str, ...] = (ID_COLUMN,)
) -> FeatureSchema:
    excluded = {TARGET_COLUMN, *quarantined}
    columns = [str(column) for column in frame if column not in excluded]
    if not columns:
        raise ValueError("No modeling features remain after quarantine")
    numeric = tuple(column for column in columns if _is_numeric(frame[column]))
    categorical = tuple(column for column in columns if column not in numeric)
    return FeatureSchema(numeric=numeric, categorical=categorical)


def prepare_features(frame: pd.DataFrame, schema: FeatureSchema) -> pd.DataFrame:
    missing = set(schema.columns) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing model features: {sorted(missing)}")
    result = frame.loc[:, list(schema.columns)].copy()
    for column in schema.numeric:
        result[column] = pd.to_numeric(result[column], errors="coerce").astype(
            "float64"
        )
    for column in schema.categorical:
        result[column] = (
            result[column].astype(object).where(result[column].notna(), float("nan"))
        )
    return result


def build_pipeline(name: str, schema: FeatureSchema, *, seed: int = 42) -> Pipeline:
    if name not in CANDIDATE_NAMES:
        raise ValueError(f"Unknown candidate {name!r}")
    numeric = Pipeline(
        [
            ("impute", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
        ]
    )
    categorical = Pipeline(
        [
            ("impute", SimpleImputer(strategy="constant", fill_value="__MISSING__")),
            ("encode", OneHotEncoder(handle_unknown="ignore")),
        ]
    )
    preprocessing = ColumnTransformer(
        [
            ("numeric", numeric, list(schema.numeric)),
            ("categorical", categorical, list(schema.categorical)),
        ],
        verbose_feature_names_out=False,
    )
    if name == "dummy":
        model: Any = DummyClassifier(strategy="stratified", random_state=seed)
    else:
        model = LogisticRegression(
            class_weight="balanced" if name == "weighted_logistic" else None,
            max_iter=2_000,
            random_state=seed,
            solver="liblinear",
        )
    return Pipeline([("preprocess", preprocessing), ("model", model)])


def _validate_grouped_folds(
    target: pd.Series[bool], groups: pd.Series[int], folds: int
) -> None:
    if folds < 2:
        raise ValueError("fold count must be at least 2")
    if len(target) != len(groups):
        raise ValueError("target and groups must have equal length")
    group_targets = pd.DataFrame({"group": groups, "target": target}).drop_duplicates()
    if group_targets.groupby("group")["target"].nunique().max() != 1:
        raise ValueError("Identical feature vectors have conflicting targets")
    class_group_counts = group_targets["target"].value_counts()
    if len(class_group_counts) != 2 or int(class_group_counts.min()) < folds:
        raise ValueError(f"Each class needs at least {folds} distinct feature groups")


def _validate_probabilities(
    target: pd.Series[bool] | NDArray[np.bool_],
    probabilities: NDArray[np.float64],
) -> tuple[NDArray[np.bool_], NDArray[np.float64]]:
    truth = np.asarray(target, dtype=bool)
    scores = np.asarray(probabilities, dtype="float64")
    if truth.ndim != 1 or scores.ndim != 1 or len(truth) != len(scores):
        raise ValueError("target and probabilities must be equal-length vectors")
    if not len(truth):
        raise ValueError("target and probabilities cannot be empty")
    if not np.isfinite(scores).all() or ((scores < 0.0) | (scores > 1.0)).any():
        raise ValueError("probabilities must be finite values between 0 and 1")
    return truth, scores


def split_train_holdout(
    frame: pd.DataFrame, *, seed: int = 42, holdout_folds: int = 5
) -> DataSplit:
    target = frame[TARGET_COLUMN].eq(MINORITY_LABEL)
    groups = feature_group_ids(frame)
    _validate_grouped_folds(target, groups, holdout_folds)
    splitter = StratifiedGroupKFold(
        n_splits=holdout_folds, shuffle=True, random_state=seed
    )
    train_index, holdout_index = next(splitter.split(frame, target, groups))
    train = frame.iloc[train_index].reset_index(drop=True)
    holdout = frame.iloc[holdout_index].reset_index(drop=True)
    return DataSplit(
        train=train,
        holdout=holdout,
        train_groups=groups.iloc[train_index].reset_index(drop=True),
        holdout_groups=groups.iloc[holdout_index].reset_index(drop=True),
    )


def metrics_at_threshold(
    target: pd.Series[bool] | NDArray[np.bool_],
    probabilities: NDArray[np.float64],
    threshold: float,
) -> BinaryMetrics:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    truth, scores = _validate_probabilities(target, probabilities)
    if np.unique(truth).size != 2:
        raise ValueError("metrics require both target classes")
    predicted = scores >= threshold
    tn, fp, fn, tp = confusion_matrix(truth, predicted, labels=[False, True]).ravel()
    return BinaryMetrics(
        pr_auc=float(average_precision_score(truth, scores)),
        roc_auc=float(roc_auc_score(truth, scores)),
        precision=float(precision_score(truth, predicted, zero_division=0)),
        recall=float(recall_score(truth, predicted, zero_division=0)),
        f1=float(f1_score(truth, predicted, zero_division=0)),
        accuracy=float(accuracy_score(truth, predicted)),
        brier=float(brier_score_loss(truth, scores)),
        true_negative=int(tn),
        false_positive=int(fp),
        false_negative=int(fn),
        true_positive=int(tp),
    )


def cross_validate_candidates(
    frame: pd.DataFrame,
    groups: pd.Series[int],
    schema: FeatureSchema,
    *,
    seed: int = 42,
    folds: int = 5,
) -> tuple[tuple[CandidateMetrics, ...], dict[str, NDArray[np.float64]]]:
    features = prepare_features(frame, schema)
    target = frame[TARGET_COLUMN].eq(MINORITY_LABEL).reset_index(drop=True)
    groups = groups.reset_index(drop=True)
    _validate_grouped_folds(target, groups, folds)
    splitter = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
    split_indices = list(splitter.split(features, target, groups))
    results: list[CandidateMetrics] = []
    probabilities_by_name: dict[str, NDArray[np.float64]] = {}
    for name in CANDIDATE_NAMES:
        probabilities = np.empty(len(frame), dtype="float64")
        fold_scores: list[float] = []
        for train_index, validation_index in split_indices:
            pipeline = build_pipeline(name, schema, seed=seed)
            pipeline.fit(features.iloc[train_index], target.iloc[train_index])
            fold_probabilities = pipeline.predict_proba(
                features.iloc[validation_index]
            )[:, 1]
            probabilities[validation_index] = fold_probabilities
            fold_scores.append(
                float(
                    average_precision_score(
                        target.iloc[validation_index], fold_probabilities
                    )
                )
            )
        probabilities_by_name[name] = probabilities
        results.append(
            CandidateMetrics(
                name=name,
                fold_pr_auc=tuple(fold_scores),
                mean_pr_auc=float(np.mean(fold_scores)),
                std_pr_auc=float(np.std(fold_scores)),
                out_of_fold_at_0_5=metrics_at_threshold(target, probabilities, 0.5),
            )
        )
    return tuple(results), probabilities_by_name


def select_candidate(candidates: tuple[CandidateMetrics, ...]) -> str:
    if {candidate.name for candidate in candidates} != set(CANDIDATE_NAMES):
        raise ValueError("Model selection requires all declared baseline candidates")
    return max(candidates, key=lambda candidate: candidate.mean_pr_auc).name


def choose_threshold(
    target: pd.Series[bool] | NDArray[np.bool_],
    probabilities: NDArray[np.float64],
    *,
    false_negative_cost: float,
    false_positive_cost: float = 1.0,
) -> ThresholdChoice:
    if false_negative_cost <= 0 or false_positive_cost <= 0:
        raise ValueError("misclassification costs must be positive")
    truth, scores = _validate_probabilities(target, probabilities)
    candidates = np.unique(np.concatenate(([0.0], scores, [1.0])))
    choices: list[ThresholdChoice] = []
    for threshold in candidates:
        predicted = scores >= threshold
        false_negative = int(np.sum(truth & ~predicted))
        false_positive = int(np.sum(~truth & predicted))
        total_cost = (
            false_negative * false_negative_cost + false_positive * false_positive_cost
        )
        choices.append(
            ThresholdChoice(
                false_negative_cost=false_negative_cost,
                false_positive_cost=false_positive_cost,
                threshold=float(threshold),
                total_cost=float(total_cost),
                false_negative=false_negative,
                false_positive=false_positive,
            )
        )
    return min(choices, key=lambda choice: (choice.total_cost, -choice.threshold))
