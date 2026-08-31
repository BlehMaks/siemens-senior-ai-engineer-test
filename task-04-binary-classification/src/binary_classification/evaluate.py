"""End-to-end Task 4 experiment runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import platform
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Any, cast

import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve  # type: ignore[import-untyped]

from binary_classification.analysis import analyze_training_frame, binary_target
from binary_classification.calibration import (
    CalibrationMetrics,
    SigmoidCalibrator,
    calibration_metrics,
    fit_grouped_sigmoid_calibrator,
)
from binary_classification.data import JoinAudit, load_training_data
from binary_classification.decision import (
    EXAMPLE_SCENARIOS,
    DecisionMetrics,
    DecisionScenario,
    evaluate_decision_policy,
    load_cost_config,
)
from binary_classification.diagnostics import (
    DiagnosticReference,
    assess_drift,
    fit_diagnostic_reference,
)
from binary_classification.modeling import (
    BinaryMetrics,
    CandidateMetrics,
    ThresholdChoice,
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

COMPARISON_SCHEMA_VERSION = "1.1"


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


def _category_slice_value(value: Any, major_values: set[str]) -> str:
    if pd.isna(value):
        return "bucket:missing"
    return f"value:{value}" if str(value) in major_values else "bucket:other"


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
        training_values = training[column].dropna().astype(str)
        major_values = set(training_values.value_counts().head(5).index)
        # Tagged display values keep literal categories distinct from report buckets.
        dimensions[column] = holdout[column].map(
            lambda value, categories=major_values: _category_slice_value(
                value, categories
            )
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


def _dataset_fingerprint(frame: pd.DataFrame) -> str:
    hashed = pd.util.hash_pandas_object(frame, index=True).to_numpy(dtype="uint64")
    return hashlib.sha256(hashed.tobytes()).hexdigest()


def _package_versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "numpy": version("numpy"),
        "pandas": version("pandas"),
        "scikit-learn": version("scikit-learn"),
    }


def _comparison_report(
    result: ExperimentResult,
    *,
    dataset_fingerprint: str,
    scenarios: tuple[DecisionScenario, ...],
    raw_calibration: CalibrationMetrics,
    calibrated_calibration: CalibrationMetrics,
    scenario_metrics: tuple[DecisionMetrics, ...],
    calibrator: SigmoidCalibrator,
    diagnostic_report: dict[str, Any],
    artifact_round_trip_parity: bool,
    runtime_seconds: float,
) -> dict[str, Any]:
    baseline = {
        "mode_status": "evaluated",
        "selected_model": result.selected_model,
        "grouped_cv": [asdict(candidate) for candidate in result.candidates],
        "baseline_threshold": result.selected_threshold,
        "holdout_at_0_5": asdict(result.holdout_at_0_5),
        "holdout_at_selected_threshold": asdict(result.holdout_at_selected_threshold),
        "probability_quality": raw_calibration.to_dict(),
    }
    extension = {
        "mode_status": "evaluated",
        "calibration": {
            "method": "grouped_oof_sigmoid",
            "parameters": calibrator.to_dict(),
            "holdout_probability_quality": calibrated_calibration.to_dict(),
            "artifact_round_trip_parity": artifact_round_trip_parity,
            "artifact_parity_tolerance": {"relative": 0.0, "absolute": 1e-12},
        },
        "decision_scenarios": [metric.to_dict() for metric in scenario_metrics],
        "diagnostics": diagnostic_report,
    }
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "metadata": {
            "dataset_fingerprint": dataset_fingerprint,
            "row_counts": {
                "training": result.training_rows,
                "holdout": result.holdout_rows,
            },
            "seed": result.seed,
            "package_versions": _package_versions(),
            "configuration": {
                "scenarios": [scenario.to_dict() for scenario in scenarios]
            },
            "runtime_seconds": runtime_seconds,
        },
        "assignment_baseline": baseline,
        "business_extension": extension,
        "delta": {
            "brier_score": (
                calibrated_calibration.brier_score - raw_calibration.brier_score
            ),
            "log_loss": calibrated_calibration.log_loss - raw_calibration.log_loss,
        },
        "limitations": [
            "Class semantics and real business costs are not provided.",
            "Bundled scenarios are examples and require owner confirmation.",
            "The holdout is evaluated only after model, calibration, and policy setup.",
            "The small minority class makes calibration and cost estimates uncertain.",
        ],
    }


def render_comparison_markdown(report: dict[str, Any]) -> str:
    """Render the human report directly from the machine-readable result object."""

    baseline = report["assignment_baseline"]
    extension = report["business_extension"]
    delta = report["delta"]
    raw = baseline["probability_quality"]
    calibrated = extension["calibration"]["holdout_probability_quality"]
    lines = [
        "# Task 4 baseline versus business extension",
        "",
        "## Assignment baseline",
        "",
        f"Selected model: `{baseline['selected_model']}`. The assignment baseline "
        "retains raw model probabilities and its predeclared threshold analysis.",
        "",
        "| Measure | Raw baseline | Calibrated extension | Delta |",
        "|---|---:|---:|---:|",
        (
            f"| Brier score | {raw['brier_score']:.6f} | "
            f"{calibrated['brier_score']:.6f} | {delta['brier_score']:+.6f} |"
        ),
        (
            f"| Log loss | {raw['log_loss']:.6f} | "
            f"{calibrated['log_loss']:.6f} | {delta['log_loss']:+.6f} |"
        ),
        (
            f"| Calibration slope | {raw['slope']:.6f} | "
            f"{calibrated['slope']:.6f} | n/a |"
        ),
        (
            f"| Calibration intercept | {raw['intercept']:.6f} | "
            f"{calibrated['intercept']:.6f} | n/a |"
        ),
        "",
        "## Business extension",
        "",
        "The extension fits a sigmoid only on grouped out-of-fold training "
        "probabilities. The untouched holdout is used for this final comparison.",
        "",
        "| Scenario | FP | FN | Review | Auto coverage | Review rate | "
        "Auto error | Expected cost | Realized cost |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metrics in extension["decision_scenarios"]:
        scenario = metrics["scenario"]
        auto_error = metrics["automatic_error_rate"]
        rendered_error = "n/a" if auto_error is None else f"{auto_error:.6f}"
        lines.append(
            f"| {scenario['name']} | {scenario['false_positive_cost']:.3f} | "
            f"{scenario['false_negative_cost']:.3f} | "
            f"{scenario['review_cost']:.3f} | "
            f"{metrics['automatic_decision_coverage']:.6f} | "
            f"{metrics['review_rate']:.6f} | {rendered_error} | "
            f"{metrics['mean_expected_cost']:.6f} | "
            f"{metrics['mean_realized_cost']:.6f} |"
        )
    diagnostics = extension["diagnostics"]
    lines.extend(
        [
            "",
            "## Training-fitted diagnostics",
            "",
            f"Status: `{diagnostics['status']}`. Thresholds are review policy, "
            "not universal drift tests.",
            "",
        ]
    )
    if diagnostics["warnings"]:
        lines.extend(
            [
                "| Warning | Feature | Observed | Policy threshold |",
                "|---|---|---:|---:|",
            ]
        )
        for warning in diagnostics["warnings"]:
            lines.append(
                f"| {warning['code']} | {warning['column']} | "
                f"{warning['observed']} | {warning['threshold']} |"
            )
    else:
        lines.append("No diagnostic policy warnings were raised.")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            *[f"- {item}" for item in report["limitations"]],
            "",
        ]
    )
    return "\n".join(lines)


def write_comparison_report(report: dict[str, Any], output_dir: str | Path) -> None:
    """Write JSON and Markdown from one in-memory comparison result."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "baseline-vs-extension.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "baseline-vs-extension.md").write_text(
        render_comparison_markdown(report), encoding="utf-8"
    )


def run_experiment(
    part1_path: str | Path,
    part2_path: str | Path,
    output_dir: str | Path,
    *,
    seed: int = 42,
    cost_scenarios: tuple[DecisionScenario, ...] = (),
) -> ExperimentResult:
    started = perf_counter()
    scenario_names = [scenario.name for scenario in cost_scenarios]
    if len(scenario_names) != len(set(scenario_names)):
        raise ValueError("cost scenario names must be unique")
    if any(
        scenario.negative_label != "y" or scenario.positive_label != "n"
        for scenario in cost_scenarios
    ):
        raise ValueError("cost scenario labels must map class_0='y' and class_1='n'")
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
    training_target = binary_target(split.train).reset_index(drop=True)
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
    diagnostic_reference: DiagnosticReference | None = None
    diagnostic_report: dict[str, Any] | None = None
    if cost_scenarios:
        diagnostic_reference = fit_diagnostic_reference(training_features, schema)
        excluded = {"Class", *training_analysis.leakage.quarantined_columns}
        diagnostic_input = split.holdout.drop(
            columns=[column for column in split.holdout if column in excluded]
        )
        diagnostic_report = assess_drift(diagnostic_reference, diagnostic_input)
        if diagnostic_report["status"] == "invalid_schema":
            raise ValueError(f"Invalid holdout schema: {diagnostic_report['schema']}")
    holdout_features = prepare_features(split.holdout, schema)
    pipeline.fit(training_features, training_target)
    holdout_probabilities = pipeline.predict_proba(holdout_features)[:, 1]
    holdout_target = binary_target(split.holdout).reset_index(drop=True)

    calibrator: SigmoidCalibrator | None = None
    calibrated_probabilities: np.ndarray[Any, Any] | None = None
    if cost_scenarios:
        folds = grouped_fold_assignments(
            split.train, split.train_groups, seed=seed, folds=5
        )
        calibrator = fit_grouped_sigmoid_calibrator(
            training_target,
            probabilities[selected_model],
            groups=split.train_groups,
            fold_assignments=folds,
        )
        calibrated_probabilities = calibrator.predict(holdout_probabilities)

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    model_path = output / "selected-model.pkl"
    artifact: dict[str, Any] = {"pipeline": pipeline, "schema": schema}
    if calibrator is not None:
        assert diagnostic_reference is not None
        artifact.update(
            {
                "artifact_schema_version": "3.0",
                "selected_model": selected_model,
                "calibrator": calibrator,
                "decision_scenarios": cost_scenarios,
                "diagnostic_reference": diagnostic_reference,
            }
        )
    with model_path.open("wb") as model_file:
        pickle.dump(artifact, model_file)
    # Parity reloads only the artifact written above; callers must not load untrusted models.
    with model_path.open("rb") as model_file:
        restored = pickle.load(model_file)
    restored_probabilities = restored["pipeline"].predict_proba(holdout_features)[:, 1]
    if not np.array_equal(holdout_probabilities, restored_probabilities):
        raise RuntimeError(
            "Serialized model predictions differ from the fitted pipeline"
        )
    artifact_round_trip_parity = True
    if calibrator is not None and calibrated_probabilities is not None:
        restored_calibrated = restored["calibrator"].predict(restored_probabilities)
        artifact_round_trip_parity = bool(
            np.allclose(
                calibrated_probabilities,
                restored_calibrated,
                rtol=0.0,
                atol=1e-12,
            )
        )
        if not artifact_round_trip_parity:
            raise RuntimeError("Serialized calibrator predictions differ after reload")

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
    if calibrator is not None and calibrated_probabilities is not None:
        assert diagnostic_report is not None
        comparison = _comparison_report(
            result,
            dataset_fingerprint=_dataset_fingerprint(dataset.frame),
            scenarios=cost_scenarios,
            raw_calibration=calibration_metrics(holdout_target, holdout_probabilities),
            calibrated_calibration=calibration_metrics(
                holdout_target, calibrated_probabilities
            ),
            scenario_metrics=tuple(
                evaluate_decision_policy(
                    holdout_target.to_numpy(dtype=bool),
                    calibrated_probabilities,
                    scenario,
                )
                for scenario in cost_scenarios
            ),
            calibrator=calibrator,
            diagnostic_report=diagnostic_report,
            artifact_round_trip_parity=artifact_round_trip_parity,
            runtime_seconds=perf_counter() - started,
        )
        write_comparison_report(comparison, output)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part1", type=Path, required=True)
    parser.add_argument("--part2", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    costs = parser.add_mutually_exclusive_group()
    costs.add_argument(
        "--cost-scenario",
        action="append",
        choices=sorted(EXAMPLE_SCENARIOS),
        help=(
            "Explicitly evaluate a bundled example cost scenario; repeat to compare "
            "examples. Values are not Siemens business truth."
        ),
    )
    costs.add_argument(
        "--cost-config",
        type=Path,
        help="Versioned JSON file containing owner-confirmed cost scenarios.",
    )
    args = parser.parse_args(argv)
    scenarios = (
        load_cost_config(args.cost_config)
        if args.cost_config is not None
        else tuple(EXAMPLE_SCENARIOS[name] for name in args.cost_scenario or ())
    )
    result = run_experiment(
        args.part1,
        args.part2,
        args.output_dir,
        seed=args.seed,
        cost_scenarios=scenarios,
    )
    print(
        f"selected={result.selected_model} "
        f"threshold={result.selected_threshold:.6f} "
        f"holdout_pr_auc={result.holdout_at_selected_threshold.pr_auc:.6f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
