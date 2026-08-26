"""Deterministic data profiling and leakage screening."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from sklearn.impute import SimpleImputer  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.metrics import average_precision_score  # type: ignore[import-untyped]
from sklearn.model_selection import (  # type: ignore[import-untyped]
    StratifiedGroupKFold,
    cross_val_predict,
)
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
from sklearn.preprocessing import (  # type: ignore[import-untyped]
    OneHotEncoder,
    StandardScaler,
)

from binary_classification.data import TARGET_VALUES, load_training_data

TARGET_COLUMN = "Class"
ID_COLUMN = "id"
MINORITY_LABEL = "n"


@dataclass(frozen=True, slots=True)
class DataProfile:
    rows: int
    feature_count: int
    class_counts: dict[str, int]
    missing_counts: dict[str, int]
    unique_counts: dict[str, int]
    feature_group_count: int
    duplicated_feature_rows: int
    duplicated_feature_groups: int
    max_feature_group_size: int
    conflicting_feature_groups: int


@dataclass(frozen=True, slots=True)
class LeakageReport:
    identifier_columns: tuple[str, ...]
    deterministic_target_columns: tuple[str, ...]
    missingness_rate_gaps: dict[str, float]
    single_feature_pr_auc: dict[str, float]
    quarantined_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    profile: DataProfile
    leakage: LeakageReport

    def to_dict(self) -> dict[str, Any]:
        profile = asdict(self.profile)
        leakage = asdict(self.leakage)
        leakage["identifier_columns"] = list(self.leakage.identifier_columns)
        leakage["deterministic_target_columns"] = list(
            self.leakage.deterministic_target_columns
        )
        leakage["quarantined_columns"] = list(self.leakage.quarantined_columns)
        return {"profile": profile, "leakage": leakage}


def _feature_columns(frame: pd.DataFrame) -> list[str]:
    return [str(column) for column in frame if column not in {TARGET_COLUMN, ID_COLUMN}]


def _is_numeric(series: pd.Series[Any]) -> bool:
    observed = series.dropna()
    return bool(
        not observed.empty and pd.to_numeric(observed, errors="coerce").notna().all()
    )


def feature_group_ids(frame: pd.DataFrame) -> pd.Series[int]:
    """Assign the same group to rows with the same complete feature vector."""

    features = _feature_columns(frame)
    group_ids, _ = pd.MultiIndex.from_frame(frame[features]).factorize(sort=False)
    return pd.Series(group_ids, index=frame.index, name="feature_group")


def binary_target(frame: pd.DataFrame) -> pd.Series[bool]:
    """Return the declared minority target after validating the public boundary."""

    if TARGET_COLUMN not in frame:
        raise ValueError(f"Frame must contain {TARGET_COLUMN!r}")
    target = frame[TARGET_COLUMN]
    observed = target.dropna().tolist()
    labels_are_strings = all(isinstance(value, str) for value in observed)
    if (
        target.isna().any()
        or not labels_are_strings
        or (labels_are_strings and frozenset(observed) != TARGET_VALUES)
    ):
        rendered = sorted({str(value) for value in observed})
        raise ValueError(
            f"Class must contain exactly {sorted(TARGET_VALUES)}, got {rendered}"
        )
    return target.eq(MINORITY_LABEL)


def _single_feature_predictions(
    frame: pd.DataFrame,
    column: str,
    target: pd.Series[bool],
    groups: pd.Series[int],
    seed: int,
) -> pd.Series[float]:
    if _is_numeric(frame[column]):
        values = pd.DataFrame(
            {column: pd.to_numeric(frame[column], errors="coerce").astype("float64")}
        )
        preprocessing: Pipeline = Pipeline(
            [
                ("impute", SimpleImputer(strategy="constant", fill_value=0.0)),
                ("scale", StandardScaler()),
            ]
        )
    else:
        values = (
            frame[[column]].astype(object).where(frame[[column]].notna(), float("nan"))
        )
        preprocessing = Pipeline(
            [
                (
                    "impute",
                    SimpleImputer(
                        strategy="most_frequent",
                        add_indicator=True,
                        keep_empty_features=True,
                    ),
                ),
                ("encode", OneHotEncoder(handle_unknown="ignore")),
            ]
        )

    group_targets = pd.DataFrame({"group": groups, "target": target}).drop_duplicates()
    if group_targets.groupby("group")["target"].nunique().max() != 1:
        raise ValueError("Identical feature vectors have conflicting targets")
    smallest_class_groups = int(group_targets["target"].value_counts().min())
    if smallest_class_groups < 2:
        raise ValueError("Leakage screening requires at least two groups per class")
    folds = min(5, smallest_class_groups)
    estimator = Pipeline(
        [
            ("preprocess", preprocessing),
            (
                "model",
                LogisticRegression(
                    max_iter=1_000, random_state=seed, solver="liblinear"
                ),
            ),
        ]
    )
    splitter = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
    probabilities = cross_val_predict(
        estimator,
        values,
        target,
        groups=groups,
        cv=splitter,
        method="predict_proba",
        n_jobs=1,
    )[:, 1]
    return pd.Series(probabilities, index=frame.index)


def analyze_training_frame(frame: pd.DataFrame, *, seed: int = 42) -> AnalysisReport:
    """Profile data and quarantine columns with implausibly predictive behavior."""

    required = {ID_COLUMN, TARGET_COLUMN}
    if not required.issubset(frame.columns):
        raise ValueError(f"Frame must contain {sorted(required)}")
    if frame.empty:
        raise ValueError("Frame must contain training rows")

    features = _feature_columns(frame)
    target = binary_target(frame)
    groups = feature_group_ids(frame)
    group_sizes = groups.value_counts()
    group_targets = pd.DataFrame({"group": groups, "target": target}).groupby("group")[
        "target"
    ]
    class_counts = {
        str(label): int(count)
        for label, count in frame[TARGET_COLUMN].value_counts().sort_index().items()
    }
    profile = DataProfile(
        rows=len(frame),
        feature_count=len(features),
        class_counts=class_counts,
        missing_counts={column: int(frame[column].isna().sum()) for column in features},
        unique_counts={
            column: int(frame[column].nunique(dropna=False)) for column in features
        },
        feature_group_count=int(group_sizes.size),
        duplicated_feature_rows=int(
            frame.duplicated(subset=features, keep=False).sum()
        ),
        duplicated_feature_groups=int((group_sizes > 1).sum()),
        max_feature_group_size=int(group_sizes.max()),
        conflicting_feature_groups=int((group_targets.nunique() > 1).sum()),
    )

    identifier_columns = tuple(
        sorted(
            [ID_COLUMN]
            + [
                column
                for column in features
                if frame[column].nunique(dropna=False) / len(frame) >= 0.98
            ]
        )
    )
    deterministic_columns: list[str] = []
    missingness_gaps: dict[str, float] = {}
    single_feature_scores: dict[str, float] = {}
    for column in features:
        missing = object()
        values = frame[column].astype(object).where(frame[column].notna(), missing)
        mapping = pd.DataFrame({"value": values, "target": target}).groupby(
            "value", dropna=False, sort=False
        )["target"]
        if mapping.nunique().max() == 1 and column not in identifier_columns:
            deterministic_columns.append(column)

        missing_by_class = frame[column].isna().groupby(target).mean()
        gap = (
            abs(float(missing_by_class.loc[True] - missing_by_class.loc[False]))
            if len(missing_by_class) == 2
            else 0.0
        )
        missingness_gaps[column] = round(gap, 6)

        predictions = _single_feature_predictions(frame, column, target, groups, seed)
        single_feature_scores[column] = round(
            float(average_precision_score(target, predictions)), 6
        )

    suspicious_scores = {
        column for column, score in single_feature_scores.items() if score >= 0.98
    }
    quarantined = tuple(
        sorted(set(identifier_columns) | set(deterministic_columns) | suspicious_scores)
    )
    leakage = LeakageReport(
        identifier_columns=identifier_columns,
        deterministic_target_columns=tuple(sorted(deterministic_columns)),
        missingness_rate_gaps=missingness_gaps,
        single_feature_pr_auc=single_feature_scores,
        quarantined_columns=quarantined,
    )
    return AnalysisReport(profile=profile, leakage=leakage)


def write_analysis(
    part1_path: str | Path, part2_path: str | Path, output_path: str | Path
) -> AnalysisReport:
    dataset = load_training_data(part1_path, part2_path)
    report = analyze_training_frame(dataset.frame)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report
