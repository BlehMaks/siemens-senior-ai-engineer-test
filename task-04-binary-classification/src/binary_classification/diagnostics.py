"""Training-fitted aggregate diagnostics for Task 4 model inputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from binary_classification.modeling import FeatureSchema, prepare_features

_QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)


@dataclass(frozen=True, slots=True)
class DiagnosticPolicy:
    """Review thresholds that are policy choices, not statistical truths."""

    missing_rate_delta_warning: float = 0.20
    numeric_quantile_shift_iqr_warning: float = 1.0
    unseen_category_rate_warning: float = 0.10

    def __post_init__(self) -> None:
        rates = (
            self.missing_rate_delta_warning,
            self.unseen_category_rate_warning,
        )
        if any(
            isinstance(value, bool) or not np.isfinite(value) or not 0.0 <= value <= 1.0
            for value in rates
        ):
            raise ValueError("rate warning thresholds must be finite values in [0, 1]")
        shift = self.numeric_quantile_shift_iqr_warning
        if isinstance(shift, bool) or not np.isfinite(shift) or shift < 0.0:
            raise ValueError("numeric shift threshold must be finite and non-negative")

    def to_dict(self) -> dict[str, float | str]:
        """Return report-safe policy values and their interpretation."""

        return {
            **asdict(self),
            "interpretation": "Review policy thresholds, not universal drift tests.",
        }


@dataclass(frozen=True, slots=True)
class NumericReference:
    column: str
    missing_rate: float
    quantiles: tuple[float, ...]
    iqr: float | None


@dataclass(frozen=True, slots=True)
class CategoricalReference:
    column: str
    missing_rate: float
    observed_values: frozenset[Any]


@dataclass(frozen=True, slots=True)
class DiagnosticReference:
    """Frozen training-only summaries; no target or holdout state is accepted."""

    schema: FeatureSchema
    training_rows: int
    policy: DiagnosticPolicy
    numeric: tuple[NumericReference, ...]
    categorical: tuple[CategoricalReference, ...]


_DEFAULT_POLICY = DiagnosticPolicy()


def _schema_drift(frame: pd.DataFrame, schema: FeatureSchema) -> dict[str, list[str]]:
    rendered = [str(column) for column in frame.columns]
    duplicate = sorted({column for column in rendered if rendered.count(column) > 1})
    required = set(schema.columns)
    observed = set(rendered)
    return {
        "missing_required_columns": sorted(required - observed),
        "duplicate_columns": duplicate,
        "unexpected_columns": sorted(observed - required),
    }


def fit_diagnostic_reference(
    training_frame: pd.DataFrame,
    schema: FeatureSchema,
    *,
    policy: DiagnosticPolicy = _DEFAULT_POLICY,
) -> DiagnosticReference:
    """Fit aggregate references from model-training features only."""

    schema_drift = _schema_drift(training_frame, schema)
    if schema_drift["missing_required_columns"] or schema_drift["duplicate_columns"]:
        raise ValueError(f"Invalid training schema: {schema_drift}")
    prepared = prepare_features(training_frame, schema)
    numeric: list[NumericReference] = []
    for column in schema.numeric:
        series = prepared[column]
        observed = series.dropna().to_numpy(dtype="float64")
        quantiles = (
            tuple(float(value) for value in np.quantile(observed, _QUANTILES))
            if len(observed)
            else ()
        )
        numeric.append(
            NumericReference(
                column=column,
                missing_rate=float(series.isna().mean()),
                quantiles=quantiles,
                iqr=(quantiles[3] - quantiles[1] if quantiles else None),
            )
        )
    categorical = tuple(
        CategoricalReference(
            column=column,
            missing_rate=float(prepared[column].isna().mean()),
            observed_values=frozenset(prepared[column].dropna().tolist()),
        )
        for column in schema.categorical
    )
    return DiagnosticReference(
        schema=schema,
        training_rows=len(prepared),
        policy=policy,
        numeric=tuple(numeric),
        categorical=categorical,
    )


def _warning(
    code: str, column: str, observed: float | str, threshold: float | str
) -> dict[str, float | str]:
    return {
        "code": code,
        "column": column,
        "observed": observed,
        "threshold": threshold,
    }


def assess_drift(
    reference: DiagnosticReference, current_frame: pd.DataFrame
) -> dict[str, Any]:
    """Return aggregate warnings without mutating or updating the reference."""

    schema = _schema_drift(current_frame, reference.schema)
    invalid = bool(schema["missing_required_columns"] or schema["duplicate_columns"])
    warnings: list[dict[str, float | str]] = [
        _warning("schema_unexpected_column", column, "present", "absent")
        for column in schema["unexpected_columns"]
    ]
    report: dict[str, Any] = {
        "mode_status": "evaluated",
        "status": "invalid_schema" if invalid else "ok",
        "reference": {
            "training_rows": reference.training_rows,
            "fitted_on": "training_features_only",
        },
        "policy": reference.policy.to_dict(),
        "schema": schema,
        "missingness": [],
        "numeric_distribution": [],
        "categorical_unseen": [],
        "warnings": warnings,
    }
    if invalid:
        return report

    prepared = prepare_features(current_frame, reference.schema)
    missingness: list[dict[str, Any]] = []
    missing_references = [
        (item.column, item.missing_rate) for item in reference.numeric
    ] + [(item.column, item.missing_rate) for item in reference.categorical]
    for column, training_rate in missing_references:
        current_rate = float(prepared[column].isna().to_numpy(dtype=bool).mean())
        shift = abs(current_rate - training_rate)
        warned = shift > reference.policy.missing_rate_delta_warning
        missingness.append(
            {
                "column": column,
                "training_rate": training_rate,
                "current_rate": current_rate,
                "absolute_shift": shift,
                "warning": warned,
            }
        )
        if warned:
            warnings.append(
                _warning(
                    "missingness_shift",
                    column,
                    shift,
                    reference.policy.missing_rate_delta_warning,
                )
            )

    numeric_distribution: list[dict[str, Any]] = []
    for numeric_ref in reference.numeric:
        observed = prepared[numeric_ref.column].dropna().to_numpy(dtype="float64")
        metric_status = "evaluated"
        shift_iqr: float | None = None
        constant_changed = False
        warned = False
        if not numeric_ref.quantiles:
            metric_status = "reference_no_observations"
        elif not len(observed):
            metric_status = "no_observations"
        elif numeric_ref.iqr == 0.0:
            constant_changed = not bool(
                np.isclose(
                    observed, numeric_ref.quantiles[2], rtol=0.0, atol=1e-12
                ).all()
            )
            shift_iqr = 0.0 if not constant_changed else None
            warned = constant_changed
        else:
            current_quantiles = np.quantile(observed, _QUANTILES)
            shift_iqr = float(
                np.max(np.abs(current_quantiles - np.asarray(numeric_ref.quantiles)))
                / numeric_ref.iqr
            )
            warned = shift_iqr > reference.policy.numeric_quantile_shift_iqr_warning
        numeric_distribution.append(
            {
                "column": numeric_ref.column,
                "method": "fixed_quantile_shift_over_training_iqr",
                "metric_status": metric_status,
                "observed_rows": len(observed),
                "shift_iqr": shift_iqr,
                "constant_reference": numeric_ref.iqr == 0.0,
                "warning": warned,
            }
        )
        if warned:
            if constant_changed:
                warning_observed: float | str = "changed"
                warning_threshold: float | str = "unchanged"
            else:
                assert shift_iqr is not None
                warning_observed = shift_iqr
                warning_threshold = reference.policy.numeric_quantile_shift_iqr_warning
            warnings.append(
                _warning(
                    "constant_numeric_changed"
                    if constant_changed
                    else "numeric_distribution_shift",
                    numeric_ref.column,
                    warning_observed,
                    warning_threshold,
                )
            )

    categorical_unseen: list[dict[str, Any]] = []
    for categorical_ref in reference.categorical:
        observed_values = prepared[categorical_ref.column].dropna().tolist()
        unseen_count = sum(
            value not in categorical_ref.observed_values for value in observed_values
        )
        unseen_rate = unseen_count / len(observed_values) if observed_values else None
        warned = bool(
            unseen_rate is not None
            and unseen_rate > reference.policy.unseen_category_rate_warning
        )
        categorical_unseen.append(
            {
                "column": categorical_ref.column,
                "observed_rows": len(observed_values),
                "unseen_count": unseen_count,
                "unseen_rate": unseen_rate,
                "warning": warned,
            }
        )
        if warned:
            assert unseen_rate is not None
            warnings.append(
                _warning(
                    "unseen_category_rate",
                    categorical_ref.column,
                    unseen_rate,
                    reference.policy.unseen_category_rate_warning,
                )
            )

    report.update(
        {
            "status": "warning" if warnings else "ok",
            "missingness": missingness,
            "numeric_distribution": numeric_distribution,
            "categorical_unseen": categorical_unseen,
            "warnings": warnings,
        }
    )
    return report
