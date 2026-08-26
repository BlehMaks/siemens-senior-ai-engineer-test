"""End-to-end Task 4 experiment runner."""

from __future__ import annotations

import argparse
import json
import pickle
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve  # type: ignore[import-untyped]

from binary_classification.analysis import analyze_training_frame
from binary_classification.data import JoinAudit, load_training_data
from binary_classification.modeling import (
    BinaryMetrics,
    CandidateMetrics,
    ThresholdChoice,
    build_pipeline,
    choose_threshold,
    cross_validate_candidates,
    infer_feature_schema,
    metrics_at_threshold,
    prepare_features,
    select_candidate,
    split_train_holdout,
)


@dataclass(frozen=True, slots=True)
class CalibrationPoint:
    mean_probability: float
    observed_rate: float


@dataclass(frozen=True, slots=True)
class ErrorSlice:
    dimension: str
    value: str
    rows: int
    false_positive: int
    false_negative: int
    precision: float
    recall: float


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    seed: int
    join_audit: JoinAudit
    training_rows: int
    holdout_rows: int
    selected_model: str
    selected_threshold: float
    candidates: tuple[CandidateMetrics, ...]
    threshold_sensitivity: tuple[ThresholdChoice, ...]
    holdout_at_0_5: BinaryMetrics
    holdout_at_selected_threshold: BinaryMetrics
    calibration: tuple[CalibrationPoint, ...]
    error_slices: tuple[ErrorSlice, ...]

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(json.dumps(asdict(self))))


def _error_slices(
    training: pd.DataFrame,
    holdout: pd.DataFrame,
    target: pd.Series[bool],
    probabilities: np.ndarray[Any, Any],
    threshold: float,
    categorical_columns: tuple[str, ...],
) -> tuple[ErrorSlice, ...]:
    predicted = probabilities >= threshold
    truth = target.to_numpy(dtype=bool)
    dimensions: dict[str, pd.Series[str]] = {}
    missing_counts = holdout.drop(columns=["id", "Class"]).isna().sum(axis=1)
    dimensions["missing_features"] = pd.Series(
        np.select(
            [missing_counts.eq(0), missing_counts.eq(1)],
            ["0", "1"],
            default="2+",
        ),
        index=holdout.index,
    )

    preferred = [column for column in ("VOL", "KAT") if column in categorical_columns]
    slice_columns = preferred or list(categorical_columns[:2])
    for column in slice_columns:
        training_values = training[column].fillna("__MISSING__").astype(str)
        major_values = set(training_values.value_counts().head(5).index)
        holdout_values = holdout[column].fillna("__MISSING__").astype(str)
        dimensions[column] = holdout_values.where(
            holdout_values.isin(major_values), "__OTHER__"
        )

    slices: list[ErrorSlice] = []
    for dimension, values in dimensions.items():
        for value in sorted(values.unique()):
            mask = values.eq(value).to_numpy()
            false_positive = int(np.sum(mask & ~truth & predicted))
            false_negative = int(np.sum(mask & truth & ~predicted))
            true_positive = int(np.sum(mask & truth & predicted))
            precision_denominator = true_positive + false_positive
            recall_denominator = true_positive + false_negative
            slices.append(
                ErrorSlice(
                    dimension=dimension,
                    value=value,
                    rows=int(mask.sum()),
                    false_positive=false_positive,
                    false_negative=false_negative,
                    precision=(
                        true_positive / precision_denominator
                        if precision_denominator
                        else 0.0
                    ),
                    recall=(
                        true_positive / recall_denominator
                        if recall_denominator
                        else 0.0
                    ),
                )
            )
    return tuple(slices)


def run_experiment(
    part1_path: str | Path,
    part2_path: str | Path,
    output_dir: str | Path,
    *,
    seed: int = 42,
) -> ExperimentResult:
    dataset = load_training_data(part1_path, part2_path)
    split = split_train_holdout(dataset.frame, seed=seed)
    training_analysis = analyze_training_frame(split.train, seed=seed)
    schema = infer_feature_schema(
        split.train, quarantined=training_analysis.leakage.quarantined_columns
    )
    candidates, probabilities = cross_validate_candidates(
        split.train, split.train_groups, schema, seed=seed
    )
    selected_model = select_candidate(candidates)
    training_target = split.train["Class"].eq("n").reset_index(drop=True)
    sensitivity = tuple(
        choose_threshold(
            training_target,
            probabilities[selected_model],
            false_negative_cost=cost,
        )
        for cost in (2.0, 5.0, 10.0)
    )
    selected_threshold = sensitivity[1].threshold

    pipeline = build_pipeline(selected_model, schema, seed=seed)
    training_features = prepare_features(split.train, schema)
    holdout_features = prepare_features(split.holdout, schema)
    pipeline.fit(training_features, training_target)
    holdout_probabilities = pipeline.predict_proba(holdout_features)[:, 1]
    holdout_target = split.holdout["Class"].eq("n").reset_index(drop=True)

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    model_path = output / "selected-model.pkl"
    with model_path.open("wb") as model_file:
        pickle.dump({"pipeline": pipeline, "schema": schema}, model_file)
    # Parity reloads only the artifact written above; callers must not load untrusted models.
    with model_path.open("rb") as model_file:
        restored = pickle.load(model_file)
    restored_probabilities = restored["pipeline"].predict_proba(holdout_features)[:, 1]
    if not np.array_equal(holdout_probabilities, restored_probabilities):
        raise RuntimeError(
            "Serialized model predictions differ from the fitted pipeline"
        )

    observed_rate, mean_probability = calibration_curve(
        holdout_target, holdout_probabilities, n_bins=10, strategy="quantile"
    )
    result = ExperimentResult(
        seed=seed,
        join_audit=dataset.audit,
        training_rows=len(split.train),
        holdout_rows=len(split.holdout),
        selected_model=selected_model,
        selected_threshold=selected_threshold,
        candidates=candidates,
        threshold_sensitivity=sensitivity,
        holdout_at_0_5=metrics_at_threshold(holdout_target, holdout_probabilities, 0.5),
        holdout_at_selected_threshold=metrics_at_threshold(
            holdout_target, holdout_probabilities, selected_threshold
        ),
        calibration=tuple(
            CalibrationPoint(float(mean), float(observed))
            for mean, observed in zip(mean_probability, observed_rate, strict=True)
        ),
        error_slices=_error_slices(
            split.train,
            split.holdout,
            holdout_target,
            holdout_probabilities,
            selected_threshold,
            schema.categorical,
        ),
    )
    (output / "metrics.json").write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part1", type=Path, required=True)
    parser.add_argument("--part2", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    result = run_experiment(args.part1, args.part2, args.output_dir, seed=args.seed)
    print(
        f"selected={result.selected_model} "
        f"threshold={result.selected_threshold:.6f} "
        f"holdout_pr_auc={result.holdout_at_selected_threshold.pr_auc:.6f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
