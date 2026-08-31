"""Optional pandas/scikit-learn adapter for multi-column consolidation."""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin  # type: ignore[import-untyped]
from sklearn.utils.validation import check_is_fitted  # type: ignore[import-untyped]

from .aliases import apply_aliases, validate_alias_maps
from .core import MISSING_CATEGORY, RareCategoryConsolidator


@dataclass(frozen=True)
class ColumnTransformDiagnostics:
    """Aggregate inference diagnostics for one transformed column."""

    row_count: int
    unseen_count: int
    unseen_rate: float
    fallback_count: int
    fallback_rate: float
    retained_category_count: int


class CategoryConsolidationTransformer(
    TransformerMixin,  # type: ignore[misc]
    BaseEstimator,  # type: ignore[misc]
):
    """Apply one training-fitted consolidator per selected DataFrame column.

    The complete DataFrame schema is frozen during ``fit``. This deliberate
    contract keeps selected and pass-through columns aligned across training and
    inference; generic ndarray estimator checks therefore do not apply.
    """

    def __init__(
        self,
        columns: Sequence[str],
        *,
        threshold_percent: float,
        min_count: int | None = None,
        rare_label: Hashable = "__RARE__",
        missing_sentinel: Hashable = MISSING_CATEGORY,
        alias_maps: Mapping[str, Mapping[Any, Any]] | None = None,
    ) -> None:
        self.columns = columns
        self.threshold_percent = threshold_percent
        self.min_count = min_count
        self.rare_label = rare_label
        self.missing_sentinel = missing_sentinel
        self.alias_maps = alias_maps

    def fit(
        self,
        X: pd.DataFrame,
        y: object = None,
    ) -> CategoryConsolidationTransformer:
        """Fit column mappings from training features only."""
        del y
        frame = _validate_frame(X)
        selected = _validate_selected_columns(self.columns, frame)

        observed_by_column = {
            column: _normalise_missing(frame[column].tolist(), self.missing_sentinel)
            for column in selected
        }
        alias_maps = validate_alias_maps(
            self.alias_maps,
            selected_columns=selected,
            observed_by_column=observed_by_column,
            rare_label=self.rare_label,
            missing_sentinel=self.missing_sentinel,
        )

        consolidators: dict[str, RareCategoryConsolidator] = {}
        for column in selected:
            values = apply_aliases(
                observed_by_column[column], alias_maps.get(column, {})
            )
            consolidators[column] = RareCategoryConsolidator(
                threshold_percent=self.threshold_percent,
                min_count=self.min_count,
                rare_label=self.rare_label,
                missing_sentinel=self.missing_sentinel,
            ).fit(values)

        self.selected_columns_ = selected
        self.feature_names_in_ = np.asarray(frame.columns, dtype=object)
        self.n_features_in_ = len(self.feature_names_in_)
        self.consolidators_ = consolidators
        self.alias_maps_ = alias_maps
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        """Transform a DataFrame while preserving its index and full schema."""
        transformed, _ = self.transform_with_diagnostics(X)
        return transformed

    def transform_with_diagnostics(
        self,
        X: pd.DataFrame,
    ) -> tuple[pd.DataFrame, dict[str, ColumnTransformDiagnostics]]:
        """Transform a DataFrame and return per-column drift summaries."""
        check_is_fitted(
            self, attributes=["alias_maps_", "consolidators_", "feature_names_in_"]
        )
        frame = _validate_frame(X)
        actual_columns = tuple(frame.columns)
        expected_columns = tuple(self.feature_names_in_.tolist())
        if actual_columns != expected_columns:
            missing = [name for name in expected_columns if name not in actual_columns]
            extra = [name for name in actual_columns if name not in expected_columns]
            raise ValueError(
                "DataFrame columns must exactly match fit schema and order; "
                f"missing={missing}, extra={extra}"
            )

        output = frame.copy()
        diagnostics: dict[str, ColumnTransformDiagnostics] = {}
        for column, consolidator in self.consolidators_.items():
            normalised_values = _normalise_missing(
                frame[column].tolist(), self.missing_sentinel
            )
            values = apply_aliases(normalised_values, self.alias_maps_.get(column, {}))
            result = consolidator.transform_with_diagnostics(values)
            output[column] = pd.Series(result.values, index=frame.index, dtype=object)
            row_count = len(result.values)
            fallback_count = sum(
                value is consolidator.resolved_rare_label
                or value == consolidator.resolved_rare_label
                for value in result.values
            )
            unseen_count = len(result.diagnostics.unseen_indexes)
            diagnostics[column] = ColumnTransformDiagnostics(
                row_count=row_count,
                unseen_count=unseen_count,
                unseen_rate=unseen_count / row_count if row_count else 0.0,
                fallback_count=fallback_count,
                fallback_rate=fallback_count / row_count if row_count else 0.0,
                retained_category_count=len(consolidator.retained_categories),
            )
        return output, diagnostics

    def get_feature_names_out(
        self,
        input_features: Sequence[str] | None = None,
    ) -> np.ndarray:
        """Return the unchanged fitted DataFrame column names."""
        check_is_fitted(self, attributes=["feature_names_in_"])
        if input_features is not None and tuple(input_features) != tuple(
            self.feature_names_in_.tolist()
        ):
            raise ValueError("input_features must match the fitted DataFrame schema")
        return self.feature_names_in_.copy()


def _validate_frame(value: object) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame):
        raise TypeError("CategoryConsolidationTransformer requires a pandas DataFrame")
    if not value.columns.is_unique:
        raise ValueError("DataFrame columns must be unique")
    if not all(isinstance(column, str) for column in value.columns):
        raise TypeError("DataFrame column names must be strings")
    return value


def _validate_selected_columns(
    columns: Sequence[str],
    frame: pd.DataFrame,
) -> tuple[str, ...]:
    if isinstance(columns, str):
        raise TypeError("columns must be a non-empty sequence of column names")
    selected = tuple(columns)
    if not selected:
        raise ValueError("columns must contain at least one column name")
    if not all(isinstance(column, str) for column in selected):
        raise TypeError("selected column names must be strings")
    if len(set(selected)) != len(selected):
        raise ValueError("selected column names must be unique")
    missing = [column for column in selected if column not in frame.columns]
    if missing:
        raise ValueError(f"selected columns are missing from DataFrame: {missing}")
    return selected


def _normalise_missing(
    values: Sequence[object],
    missing_sentinel: Hashable,
) -> list[object]:
    normalised: list[object] = []
    for value in values:
        missing = pd.isna(value)  # type: ignore[call-overload]
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            normalised.append(missing_sentinel)
        else:
            normalised.append(value)
    return normalised
