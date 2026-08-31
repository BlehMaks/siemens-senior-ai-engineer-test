"""Generate the deterministic Task 6 baseline-versus-extension report."""

from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
import tracemalloc
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

import pandas as pd
from sklearn.base import clone  # type: ignore[import-untyped]
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]

from .artifact import artifact_fingerprint, dump_mapping_artifact, load_mapping_artifact
from .core import MISSING_CATEGORY, consolidate_rare_categories
from .sklearn import CategoryConsolidationTransformer, ColumnTransformDiagnostics

REPORT_SCHEMA_VERSION = 1
FIXTURE_SEED = 0
DEFAULT_REPORT_DIR = Path(__file__).resolve().parents[2] / "reports"


def build_comparison_report(
    *,
    benchmark_rows: int = 5_000,
    benchmark_iterations: int = 5,
) -> dict[str, Any]:
    """Evaluate the baseline and extension on one sanitized fixture."""
    if not 1 <= benchmark_rows <= 100_000:
        raise ValueError("benchmark_rows must be from 1 to 100000")
    if not 1 <= benchmark_iterations <= 20:
        raise ValueError("benchmark_iterations must be from 1 to 20")

    started = perf_counter()
    training = _training_fixture()
    inference = _inference_fixture()
    selected = ("region", "channel")
    threshold_percent = 15.0
    min_count = 3

    percent_transformer = CategoryConsolidationTransformer(
        columns=selected,
        threshold_percent=threshold_percent,
    ).fit(training)
    percent_output, percent_diagnostics = (
        percent_transformer.transform_with_diagnostics(inference)
    )
    baseline_region = consolidate_rare_categories(
        training["region"].where(training["region"].notna(), MISSING_CATEGORY).tolist(),
        threshold_percent,
        missing_sentinel=MISSING_CATEGORY,
    )
    percent_region = percent_transformer.transform(training)["region"].tolist()
    baseline_equivalent = baseline_region == percent_region

    dual_transformer = CategoryConsolidationTransformer(
        columns=selected,
        threshold_percent=threshold_percent,
        min_count=min_count,
    ).fit(training)
    dual_output, dual_diagnostics = dual_transformer.transform_with_diagnostics(
        inference
    )
    artifact = dump_mapping_artifact(dual_transformer)
    restored = load_mapping_artifact(artifact)

    cloned = clone(dual_transformer)
    clone_parameters_match = cloned.get_params() == dual_transformer.get_params()
    cloned.set_params(min_count=2)
    set_params_supported = cloned.get_params()["min_count"] == 2
    pipeline = Pipeline([("categories", clone(dual_transformer))]).fit(training)
    pipeline_stable = pipeline.transform(inference).equals(dual_output)
    pickle_stable = (
        pickle.loads(pickle.dumps(dual_transformer))
        .transform(inference)
        .equals(dual_output)
    )
    artifact_stable = restored.transform(inference).equals(dual_output)
    alias_evidence = _evaluate_alias_normalization()

    percent_stats = _column_stats(
        input_frame=inference,
        output_frame=percent_output,
        diagnostics=percent_diagnostics,
    )
    dual_stats = _column_stats(
        input_frame=inference,
        output_frame=dual_output,
        diagnostics=dual_diagnostics,
    )
    mapping_differences = {
        column: sorted(
            (
                percent_transformer.consolidators_[column].retained_categories
                - dual_transformer.consolidators_[column].retained_categories
            ),
            key=repr,
        )
        for column in selected
    }
    benchmark = _run_microbenchmark(
        row_count=benchmark_rows,
        iterations=benchmark_iterations,
        threshold_percent=threshold_percent,
        min_count=min_count,
    )
    runtime_seconds = perf_counter() - started

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "metadata": {
            "data_fingerprint": _fixture_fingerprint(training, inference),
            "training_rows": len(training),
            "inference_rows": len(inference),
            "seed": FIXTURE_SEED,
            "package_versions": {
                "python": sys.version.split()[0],
                "pandas": pd.__version__,
                "scikit_learn": _installed_version("scikit-learn"),
                "task_6": _installed_version("siemens-category-consolidation"),
            },
            "configuration": {
                "columns": list(selected),
                "threshold_percent": threshold_percent,
                "min_count": min_count,
            },
            "runtime_seconds": runtime_seconds,
        },
        "assignment_baseline": {
            "mode_status": "evaluated",
            "description": "Standalone single-column percentage-only helper.",
            "single_column_output_equivalent": baseline_equivalent,
            "by_column": percent_stats,
        },
        "business_extension": {
            "mode_status": "evaluated",
            "description": "Opt-in multi-column percentage plus minimum-count policy.",
            "by_column": dual_stats,
            "mapping_differences_from_percent_only": mapping_differences,
            "sklearn_checks": {
                "fit_transform": True,
                "get_feature_names_out": (
                    dual_transformer.get_feature_names_out().tolist()
                    == training.columns.tolist()
                ),
                "pandas_output": isinstance(dual_output, pd.DataFrame),
                "clone": clone_parameters_match,
                "get_params_set_params": set_params_supported,
                "pipeline": pipeline_stable,
                "pickle_runtime_check": pickle_stable,
                "safe_json_artifact": artifact_stable,
            },
            "artifact": {
                "schema_version": 1,
                "fingerprint": artifact_fingerprint(artifact),
            },
            "microbenchmark": benchmark,
            "alias_normalization": alias_evidence,
        },
        "delta": {
            "comparable_measures": {
                column: {
                    "fallback_count": (
                        dual_stats[column]["fallback_count"]
                        - percent_stats[column]["fallback_count"]
                    ),
                    "retained_category_count": (
                        dual_stats[column]["retained_category_count"]
                        - percent_stats[column]["retained_category_count"]
                    ),
                }
                for column in selected
            }
        },
        "limitations": [
            "The fixture is sanitized engineering evidence, not production data.",
            "Runtime and peak memory vary by machine and are not universal promises.",
            "Aliases are exact reviewed mappings; no fuzzy or case matching is used.",
        ],
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    """Render the human report directly from the machine report object."""
    baseline = report["assignment_baseline"]
    extension = report["business_extension"]
    metadata = report["metadata"]
    lines = [
        "# Task 6 baseline versus business extension",
        "",
        f"Schema version: `{report['schema_version']}`",
        "",
        f"Fixture fingerprint: `{metadata['data_fingerprint']}`",
        "",
        f"Training/inference rows: `{metadata['training_rows']}` / "
        f"`{metadata['inference_rows']}`",
        "",
        "## Assignment baseline",
        "",
        baseline["description"],
        "",
        "Single-column output equivalence with the percent-only adapter: "
        f"`{str(baseline['single_column_output_equivalent']).lower()}`.",
        "",
        _render_column_table(baseline["by_column"]),
        "",
        "## Business extension",
        "",
        extension["description"],
        "",
        _render_column_table(extension["by_column"]),
        "",
        "The safe mapping artifact uses schema version "
        f"`{extension['artifact']['schema_version']}` with fingerprint "
        f"`{extension['artifact']['fingerprint']}`.",
        "",
        "All recorded sklearn checks passed: "
        f"`{str(all(extension['sklearn_checks'].values())).lower()}`.",
        "",
        "### Reviewed alias normalization",
        "",
        "Alias normalization is "
        f"`{extension['alias_normalization']['mode_status']}` and disabled by "
        "default. The declared spelling variant "
        f"`{extension['alias_normalization']['declared_variant']}` maps to "
        f"`{extension['alias_normalization']['declared_variant_output']}`; the "
        "undeclared case variant remains unseen and maps to "
        f"`{extension['alias_normalization']['undeclared_variant_output']}`.",
        "",
        "Canonical training count after normalization: "
        f"`{extension['alias_normalization']['canonical_training_count']}`. "
        "The alias artifact uses schema version "
        f"`{extension['alias_normalization']['artifact']['schema_version']}` with "
        "deterministic round-trip parity: "
        f"`{str(extension['alias_normalization']['artifact']['round_trip']).lower()}`.",
        "",
        "## Bounded microbenchmark",
        "",
        f"Rows / iterations: `{extension['microbenchmark']['row_count']}` / "
        f"`{extension['microbenchmark']['iterations']}`. Median core time: "
        f"`{extension['microbenchmark']['core_median_seconds']:.6f}s`; median adapter "
        f"time: `{extension['microbenchmark']['adapter_median_seconds']:.6f}s`; peak "
        f"memory: `{extension['microbenchmark']['peak_memory_bytes']}` bytes.",
        "",
        "These measurements are bounded engineering evidence, not a universal "
        "performance promise.",
        "",
        "## Limitations",
        "",
        *[f"- {limitation}" for limitation in report["limitations"]],
        "",
    ]
    return "\n".join(lines)


def write_comparison_report(
    output_dir: Path,
    *,
    benchmark_rows: int = 5_000,
    benchmark_iterations: int = 5,
) -> tuple[Path, Path]:
    """Write JSON and generated Markdown from one evaluation result."""
    report = build_comparison_report(
        benchmark_rows=benchmark_rows,
        benchmark_iterations=benchmark_iterations,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "baseline-vs-extension.json"
    markdown_path = output_dir / "baseline-vs-extension.md"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def main(argv: list[str] | None = None) -> int:
    """Run the sanitized Task 6 comparison from the command line."""
    parser = argparse.ArgumentParser(
        description="Generate the Task 6 baseline-versus-extension fixture report."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--benchmark-rows", type=int, default=5_000)
    parser.add_argument("--benchmark-iterations", type=int, default=5)
    args = parser.parse_args(argv)
    json_path, markdown_path = write_comparison_report(
        args.output_dir,
        benchmark_rows=args.benchmark_rows,
        benchmark_iterations=args.benchmark_iterations,
    )
    print(f"Wrote {json_path}")
    print(f"Wrote {markdown_path}")
    return 0


def _training_fixture() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "region": [
                "north",
                "north",
                "north",
                "north",
                "north",
                "south",
                "south",
                "south",
                "east",
                "east",
                "west",
                None,
            ],
            "channel": [
                "web",
                "web",
                "web",
                "web",
                "web",
                "web",
                "store",
                "store",
                "store",
                "partner",
                "partner",
                "phone",
            ],
            "value": list(range(12)),
        }
    )


def _inference_fixture() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "region": ["north", "south", "east", "west", "central", None],
            "channel": ["web", "store", "partner", "phone", "mobile", "web"],
            "value": [20, 21, 22, 23, 24, 25],
        }
    )


def _evaluate_alias_normalization() -> dict[str, object]:
    training = pd.DataFrame(
        {"region": ["north", "north", "nroth", "south"], "value": range(4)}
    )
    inference = pd.DataFrame({"region": ["nroth", "North", "north"], "value": range(3)})
    transformer = CategoryConsolidationTransformer(
        columns=("region",),
        threshold_percent=60.0,
        alias_maps={"region": {"nroth": "north"}},
    ).fit(training)
    training_output = transformer.transform(training)
    output, diagnostics = transformer.transform_with_diagnostics(inference)
    artifact = dump_mapping_artifact(transformer)
    restored = load_mapping_artifact(artifact)
    artifact_document = json.loads(artifact)
    region = transformer.consolidators_["region"]
    return {
        "mode_status": "evaluated",
        "enabled_by_default": False,
        "matching": "exact_explicit_map_only",
        "target_values_used": False,
        "policy": {"region": {"nroth": "north"}},
        "declared_variant": "nroth",
        "declared_variant_output": output.loc[0, "region"],
        "undeclared_variant": "North",
        "undeclared_variant_output": output.loc[1, "region"],
        "undeclared_variant_unseen": diagnostics["region"].unseen_count == 1,
        "canonical_training_count": int((training_output["region"] == "north").sum()),
        "canonical_retained": "north" in region.retained_categories,
        "artifact": {
            "schema_version": artifact_document["schema_version"],
            "fingerprint": artifact_fingerprint(artifact),
            "round_trip": restored.transform(inference).equals(output),
        },
    }


def _column_stats(
    *,
    input_frame: pd.DataFrame,
    output_frame: pd.DataFrame,
    diagnostics: Mapping[str, ColumnTransformDiagnostics],
) -> dict[str, dict[str, int | float]]:
    stats: dict[str, dict[str, int | float]] = {}
    for column, summary in diagnostics.items():
        stats[column] = {
            "category_count_before": int(input_frame[column].nunique(dropna=False)),
            "category_count_after": int(output_frame[column].nunique(dropna=False)),
            "fallback_count": summary.fallback_count,
            "fallback_rate": summary.fallback_rate,
            "unseen_count": summary.unseen_count,
            "unseen_rate": summary.unseen_rate,
            "retained_category_count": summary.retained_category_count,
        }
    return stats


def _run_microbenchmark(
    *,
    row_count: int,
    iterations: int,
    threshold_percent: float,
    min_count: int,
) -> dict[str, int | float]:
    values = [f"category-{index % 20}" for index in range(row_count)]
    frame = pd.DataFrame({"category": values, "passthrough": range(row_count)})
    core_times: list[float] = []
    adapter_times: list[float] = []
    tracemalloc.start()
    for _ in range(iterations):
        started = perf_counter()
        consolidate_rare_categories(
            values,
            threshold_percent,
            min_count=min_count,
        )
        core_times.append(perf_counter() - started)

        started = perf_counter()
        CategoryConsolidationTransformer(
            columns=("category",),
            threshold_percent=threshold_percent,
            min_count=min_count,
        ).fit_transform(frame)
        adapter_times.append(perf_counter() - started)
    _, peak_memory_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    core_median = median(core_times)
    adapter_median = median(adapter_times)
    return {
        "row_count": row_count,
        "iterations": iterations,
        "core_median_seconds": core_median,
        "adapter_median_seconds": adapter_median,
        "adapter_overhead_ratio": adapter_median / core_median if core_median else 0.0,
        "peak_memory_bytes": peak_memory_bytes,
    }


def _fixture_fingerprint(training: pd.DataFrame, inference: pd.DataFrame) -> str:
    payload = {
        "training": training.where(training.notna(), None).to_dict(orient="list"),
        "inference": inference.where(inference.notna(), None).to_dict(orient="list"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _installed_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "source-tree"


def _render_column_table(by_column: dict[str, dict[str, object]]) -> str:
    rows = [
        "| Column | Categories before/after | Fallback count/rate | "
        "Unseen count/rate | Retained |",
        "|---|---:|---:|---:|---:|",
    ]
    for column in sorted(by_column):
        metrics = by_column[column]
        rows.append(
            f"| `{column}` | {metrics['category_count_before']} / "
            f"{metrics['category_count_after']} | {metrics['fallback_count']} / "
            f"{metrics['fallback_rate']:.3f} | {metrics['unseen_count']} / "
            f"{metrics['unseen_rate']:.3f} | {metrics['retained_category_count']} |"
        )
    return "\n".join(rows)


if __name__ == "__main__":
    raise SystemExit(main())
