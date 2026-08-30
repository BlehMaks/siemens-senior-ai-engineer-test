"""Deterministic evaluation of reviewed material-relevance judgments."""

from __future__ import annotations

import argparse
import json
import platform
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import file_digest
from importlib.metadata import PackageNotFoundError, version
from math import log2
from pathlib import Path
from time import perf_counter
from typing import Literal, cast

from material_similarity.data import (
    DESCRIPTION_COLUMN,
    MATERIAL_COLUMNS,
    PART_ID_COLUMN,
    load_materials,
    profile_materials,
)
from material_similarity.hybrid import (
    DEFAULT_COMPATIBILITY_POLICY,
    BusinessRetrievalResult,
    CompatibilityPolicy,
    HybridRetrievalResult,
    assess_compatibility,
    parse_material_attributes,
    rank_business_alternatives,
    rank_hybrid_alternatives,
)
from material_similarity.normalize import normalize_description
from material_similarity.retrieval import (
    TOP_K,
    RetrievalResult,
    RetrievalStatus,
    rank_alternatives,
)

WEIGHT_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
RELEVANT_GRADE = 2
_EXPECTED_STATUSES = frozenset(
    {"ok", "insufficient_description", "insufficient_candidates"}
)


class BenchmarkError(ValueError):
    """Raised when reviewed labels cannot support a trustworthy evaluation."""


@dataclass(frozen=True)
class Judgment:
    """One reviewed candidate with a graded, human-readable rationale."""

    part_id: str
    grade: int
    rationale: str


@dataclass(frozen=True)
class BenchmarkQuery:
    """One query and the complete reviewed pool needed by the weight grid."""

    part_id: str
    expected_status: RetrievalStatus
    slices: tuple[str, ...]
    rationale: str
    judgments: tuple[Judgment, ...]


@dataclass(frozen=True)
class Benchmark:
    """Versioned relevance labels tied to an exact source catalog."""

    catalog_sha256: str
    catalog_row_count: int
    queries: tuple[BenchmarkQuery, ...]


@dataclass(frozen=True)
class Metrics:
    """Macro retrieval metrics over one benchmark view."""

    precision_at_5: float
    ndcg_at_5: float
    coverage: float
    expected_status_rate: float
    query_count: int
    ranked_query_count: int


@dataclass(frozen=True)
class SliceMetrics:
    """Metrics for one declared qualitative failure slice."""

    name: str
    metrics: Metrics


@dataclass(frozen=True)
class WeightEvaluation:
    """Reviewed metrics for one word/character channel weighting."""

    word_weight: float
    character_weight: float
    metrics: Metrics
    slices: tuple[SliceMetrics, ...]


@dataclass(frozen=True)
class EvaluationReport:
    """Comparable weight trials plus stability of the selected configuration."""

    selected_word_weight: float
    selected_character_weight: float
    stability: float
    evaluations: tuple[WeightEvaluation, ...]


@dataclass(frozen=True)
class HybridEvaluationReport:
    """Baseline comparison and the explicit hybrid promotion decision."""

    text_metrics: Metrics
    hybrid_metrics: Metrics
    text_hard_negative_rate: float
    hybrid_hard_negative_rate: float
    hybrid_stability: float
    promoted: bool
    non_promotion_reasons: tuple[str, ...]


@dataclass(frozen=True)
class SafetyCase:
    """One reviewed pairwise compatibility expectation."""

    case_id: str
    split: Literal["training", "held_out"]
    rule: str
    tags: tuple[str, ...]
    query: Mapping[str, str]
    candidate: Mapping[str, str]
    expected_outcome: str
    expected_codes: tuple[str, ...]


@dataclass(frozen=True)
class SafetyBenchmark:
    """Bounded reviewed safety cases independent of the private catalog."""

    provenance: str
    reviewer_status: str
    cases: tuple[SafetyCase, ...]


@dataclass(frozen=True)
class SafetyCaseResult:
    """Observed outcome for one immutable safety case."""

    case_id: str
    split: str
    rule: str
    expected_outcome: str
    actual_outcome: str
    expected_codes: tuple[str, ...]
    actual_codes: tuple[str, ...]
    passed: bool


def load_benchmark(
    path: Path,
    materials: Sequence[Mapping[str, str]],
    *,
    catalog_sha256: str = "",
) -> Benchmark:
    """Load JSON-compatible YAML and validate every label against the catalog."""

    if len(catalog_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in catalog_sha256
    ):
        raise BenchmarkError(
            "catalog SHA-256 must be 64 lowercase hexadecimal characters"
        )
    profile_materials(materials)
    try:
        root = _object(json.loads(path.read_text(encoding="utf-8")), "benchmark")
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkError(f"cannot read benchmark: {error}") from error

    if _integer(root.get("version"), "version") != 1:
        raise BenchmarkError("unsupported benchmark version")
    catalog = _object(root.get("catalog"), "catalog")
    expected_sha256 = _text(catalog.get("sha256"), "catalog.sha256")
    expected_rows = _integer(catalog.get("row_count"), "catalog.row_count")
    if expected_rows != len(materials):
        raise BenchmarkError("benchmark catalog row count does not match input")
    if expected_sha256 != catalog_sha256:
        raise BenchmarkError("benchmark catalog SHA-256 does not match input")

    rows_by_id = {material[PART_ID_COLUMN].strip(): material for material in materials}
    queries = tuple(
        _query(item, rows_by_id, index)
        for index, item in enumerate(_array(root.get("queries"), "queries"))
    )
    query_ids = [query.part_id for query in queries]
    if not queries:
        raise BenchmarkError("benchmark has no queries")
    if len(set(query_ids)) != len(query_ids):
        raise BenchmarkError("benchmark contains duplicate query IDs")
    return Benchmark(expected_sha256, expected_rows, queries)


def load_safety_benchmark(path: Path) -> SafetyBenchmark:
    """Load the bounded JSON-compatible YAML safety benchmark."""

    try:
        root = _object(json.loads(path.read_text(encoding="utf-8")), "safety benchmark")
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkError(f"cannot read safety benchmark: {error}") from error
    if root.get("schema_version") != "1.0":
        raise BenchmarkError("unsupported safety benchmark schema version")
    provenance = _text(root.get("provenance"), "provenance")
    reviewer_status = _text(root.get("reviewer_status"), "reviewer_status")
    if reviewer_status != "reviewed":
        raise BenchmarkError("safety benchmark must be reviewed")
    defaults = _string_mapping(root.get("defaults"), "defaults")
    raw_cases = _array(root.get("cases"), "cases")
    if not 1 <= len(raw_cases) <= 24:
        raise BenchmarkError("safety benchmark must contain between 1 and 24 cases")
    cases = tuple(
        _safety_case(value, defaults, index) for index, value in enumerate(raw_cases)
    )
    case_ids = tuple(case.case_id for case in cases)
    if len(set(case_ids)) != len(case_ids):
        raise BenchmarkError("safety benchmark contains duplicate case IDs")
    if {case.split for case in cases} != {"training", "held_out"}:
        raise BenchmarkError(
            "safety benchmark must separate training and held-out cases"
        )
    required_rules = {
        "current",
        "ac_voltage",
        "dc_voltage",
        "dimensions",
        "acting",
        "material",
        "mounting",
        "mounting_feature",
    }
    for rule in required_rules:
        outcomes = {case.expected_outcome for case in cases if case.rule == rule}
        if "compatible" not in outcomes or not outcomes & {
            "conflict",
            "insufficient_evidence",
        }:
            raise BenchmarkError(
                f"safety benchmark lacks supported and negative cases for {rule}"
            )
    required_tags = {
        "blank_description",
        "duplicate_description",
        "hard_conflict",
        "must_abstain",
        "parser_failure",
        "sparse_row",
        "strict_candidate",
    }
    observed_tags = {tag for case in cases for tag in case.tags}
    missing_tags = sorted(required_tags - observed_tags)
    if missing_tags:
        raise BenchmarkError(f"safety benchmark lacks required cases: {missing_tags}")
    return SafetyBenchmark(provenance, reviewer_status, cases)


def evaluate_safety_benchmark(
    benchmark: SafetyBenchmark,
    *,
    policy: CompatibilityPolicy = DEFAULT_COMPATIBILITY_POLICY,
) -> tuple[SafetyCaseResult, ...]:
    """Evaluate all declared safety expectations without relevance certification."""

    results: list[SafetyCaseResult] = []
    for case in benchmark.cases:
        assessment = assess_compatibility(case.query, case.candidate, policy=policy)
        actual_codes = tuple(
            sorted(
                {
                    *(conflict.code for conflict in assessment.conflicts),
                    *(
                        f"unsupported:{item.field}:{item.side}"
                        for item in assessment.unsupported
                    ),
                }
            )
        )
        expected_codes = tuple(sorted(case.expected_codes))
        results.append(
            SafetyCaseResult(
                case_id=case.case_id,
                split=case.split,
                rule=case.rule,
                expected_outcome=case.expected_outcome,
                actual_outcome=assessment.outcome,
                expected_codes=expected_codes,
                actual_codes=actual_codes,
                passed=(
                    assessment.outcome == case.expected_outcome
                    and set(expected_codes).issubset(actual_codes)
                ),
            )
        )
    return tuple(results)


def build_comparison_report(
    materials: Sequence[Mapping[str, str]],
    benchmark: Benchmark,
    safety_benchmark: SafetyBenchmark,
    *,
    dataset_fingerprint: str,
    policy: CompatibilityPolicy = DEFAULT_COMPATIBILITY_POLICY,
    runtime_seconds: tuple[float, float] | None = None,
) -> dict[str, object]:
    """Evaluate baseline and opt-in modes once and return their shared report object."""

    if len(dataset_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in dataset_fingerprint
    ):
        raise ValueError("dataset fingerprint must be a lowercase SHA-256")
    profile = profile_materials(materials)
    baseline_started = perf_counter()
    baseline_results = rank_alternatives(materials)
    baseline_seconds = perf_counter() - baseline_started
    extension_started = perf_counter()
    extension_results = rank_business_alternatives(materials, policy=policy)
    extension_seconds = perf_counter() - extension_started
    if runtime_seconds is not None:
        if len(runtime_seconds) != 2 or any(value < 0.0 for value in runtime_seconds):
            raise ValueError("runtime override must contain two non-negative values")
        baseline_seconds, extension_seconds = runtime_seconds

    rows_by_id = {material[PART_ID_COLUMN].strip(): material for material in materials}
    nonblank_queries = tuple(
        query
        for query in benchmark.queries
        if normalize_description(rows_by_id[query.part_id][DESCRIPTION_COLUMN])
    )
    if not nonblank_queries:
        raise BenchmarkError("comparison benchmark has no non-blank extension queries")
    baseline_by_id = _results_by_id(baseline_results)
    extension_by_id = {result.part_id: result for result in extension_results}
    strict_retrieval = tuple(
        _business_as_retrieval(extension_by_id[query.part_id])
        for query in nonblank_queries
    )
    strict_metrics = _score_queries(
        nonblank_queries,
        _results_by_id(strict_retrieval),
    )
    comparable_baseline_metrics = _score_queries(nonblank_queries, baseline_by_id)
    baseline_metrics = score_results(benchmark, baseline_results)
    safety_results = evaluate_safety_benchmark(safety_benchmark, policy=policy)
    strict = tuple(
        result for result in extension_results if result.mode == "strict_hybrid"
    )
    structured = tuple(
        result for result in extension_results if result.mode == "structured_only"
    )
    parser_support, parser_failures = _parser_summary(materials, policy)
    mode_counts = Counter(result.status for result in extension_results)
    strict_hard_negative_rate = _hard_negative_rate(
        nonblank_queries,
        _results_by_id(strict_retrieval),
    )
    baseline_hard_negative_rate = _hard_negative_rate(
        nonblank_queries,
        baseline_by_id,
    )
    safety_passed = sum(result.passed for result in safety_results)
    limitations = [
        "Compatibility labels validate rule behavior; they do not certify electrical interchangeability.",
        "Structured-only precision@5 and nDCG@5 are not reported without reviewed relevance labels for blank-description rows.",
        "The batch API does not expose reliable per-query p50/p95 latency; those fields remain null.",
        "The bundled policy requires engineering-owner confirmation before operational use.",
    ]
    report: dict[str, object] = {
        "schema_version": "1.0",
        "metadata": {
            "dataset_fingerprint": dataset_fingerprint,
            "row_count": profile.row_count,
            "blank_description_count": profile.blank_description_count,
            "benchmark_query_count": len(benchmark.queries),
            "seed": None,
            "runtime_seconds": {
                "assignment_baseline_batch": round(baseline_seconds, 6),
                "business_extension_batch": round(extension_seconds, 6),
                "per_query_p50": None,
                "per_query_p95": None,
            },
            "package_versions": {
                "python": platform.python_version(),
                "pandas": _installed_version("pandas"),
                "scikit-learn": _installed_version("scikit-learn"),
            },
            "configuration": {"compatibility_policy": policy.to_dict()},
        },
        "assignment_baseline": {
            "mode": "lexical_v1",
            "mode_status": "evaluated",
            "metrics": asdict(baseline_metrics),
            "comparable_nonblank_metrics": asdict(comparable_baseline_metrics),
            "reviewed_hard_negative_rate": baseline_hard_negative_rate,
        },
        "business_extension": {
            "mode_status": "evaluated",
            "strict_hybrid": {
                "mode_status": "evaluated",
                "eligible_case_count": len(strict),
                "metrics": asdict(strict_metrics),
                "reviewed_hard_negative_rate": strict_hard_negative_rate,
                "exactly_five_coverage": _status_rate(strict, "ok"),
                "defensible_candidate_shortfall": sum(
                    max(0, 5 - len(result.alternatives)) for result in strict
                ),
            },
            "structured_only": {
                "mode_status": "evaluated",
                "eligible_case_count": len(structured),
                "precision_at_5": None,
                "ndcg_at_5": None,
                "exactly_five_coverage": _status_rate(structured, "ok"),
                "defensible_candidate_shortfall": sum(
                    max(0, 5 - len(result.alternatives)) for result in structured
                ),
            },
            "relaxed_hybrid": {"mode_status": "not_implemented"},
            "status_counts": dict(sorted(mode_counts.items())),
            "safety_benchmark": {
                "case_count": len(safety_results),
                "passed_count": safety_passed,
                "failed_count": len(safety_results) - safety_passed,
                "held_out_case_count": sum(
                    result.split == "held_out" for result in safety_results
                ),
                "representative_results": [
                    asdict(result)
                    for result in safety_results
                    if result.case_id
                    in {"current-supported", "current-conflict", "sparse-abstention"}
                ],
            },
            "parser_support": parser_support,
            "parser_failures": parser_failures,
            "review_workload": {
                "candidates_rejected_automatically": sum(
                    len(result.excluded) for result in extension_results
                ),
                "cases_requiring_review": mode_counts["review_required"],
                "cases_without_evidence_backed_result": mode_counts[
                    "insufficient_evidence"
                ],
            },
        },
        "delta": {
            "precision_at_5": round(
                strict_metrics.precision_at_5
                - comparable_baseline_metrics.precision_at_5,
                6,
            ),
            "ndcg_at_5": round(
                strict_metrics.ndcg_at_5 - comparable_baseline_metrics.ndcg_at_5,
                6,
            ),
            "reviewed_hard_negative_rate": round(
                strict_hard_negative_rate - baseline_hard_negative_rate,
                6,
            ),
        },
        "limitations": limitations,
    }
    return cast(
        dict[str, object],
        json.loads(json.dumps(report, ensure_ascii=False, sort_keys=True)),
    )


def render_comparison_markdown(report: Mapping[str, object]) -> str:
    """Render the human report exclusively from the machine-readable object."""

    metadata = cast(Mapping[str, object], report["metadata"])
    baseline = cast(Mapping[str, object], report["assignment_baseline"])
    extension = cast(Mapping[str, object], report["business_extension"])
    baseline_metrics = cast(Mapping[str, object], baseline["metrics"])
    strict = cast(Mapping[str, object], extension["strict_hybrid"])
    strict_metrics = cast(Mapping[str, object], strict["metrics"])
    structured = cast(Mapping[str, object], extension["structured_only"])
    safety = cast(Mapping[str, object], extension["safety_benchmark"])
    workload = cast(Mapping[str, object], extension["review_workload"])
    limitations = cast(Sequence[str], report["limitations"])
    lines = [
        "# Task 5 baseline versus business extension",
        "",
        f"- Schema version: `{report['schema_version']}`",
        f"- Dataset fingerprint: `{metadata['dataset_fingerprint']}`",
        f"- Catalog rows: {metadata['row_count']}",
        f"- Blank descriptions: {metadata['blank_description_count']}",
        "",
        "## Mode comparison",
        "",
        "| Mode | Status | Eligible/queries | Precision@5 | nDCG@5 | Exactly-five coverage |",
        "|---|---|---:|---:|---:|---:|",
        f"| Lexical v1 | {baseline['mode_status']} | {baseline_metrics['query_count']} | {baseline_metrics['precision_at_5']} | {baseline_metrics['ndcg_at_5']} | {baseline_metrics['coverage']} |",
        f"| Strict hybrid v2 | {strict['mode_status']} | {strict['eligible_case_count']} | {strict_metrics['precision_at_5']} | {strict_metrics['ndcg_at_5']} | {strict['exactly_five_coverage']} |",
        f"| Structured only v2 | {structured['mode_status']} | {structured['eligible_case_count']} | not evaluated | not evaluated | {structured['exactly_five_coverage']} |",
        f"| Relaxed hybrid | {cast(Mapping[str, object], extension['relaxed_hybrid'])['mode_status']} | 0 | not evaluated | not evaluated | not evaluated |",
        "",
        "## Safety and review workload",
        "",
        f"- Reviewed safety cases passed: {safety['passed_count']}/{safety['case_count']}",
        f"- Automatically rejected candidates: {workload['candidates_rejected_automatically']}",
        f"- Cases requiring review: {workload['cases_requiring_review']}",
        f"- Cases without an evidence-backed result: {workload['cases_without_evidence_backed_result']}",
        "",
        "## Limitations",
        "",
        *(f"- {item}" for item in limitations),
        "",
    ]
    return "\n".join(lines)


def write_comparison_report(
    report: Mapping[str, object],
    json_path: Path,
    markdown_path: Path,
) -> None:
    """Write deterministic JSON and Markdown views of the same report object."""

    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_comparison_markdown(report), encoding="utf-8")


def score_results(
    benchmark: Benchmark,
    results: Sequence[RetrievalResult],
) -> Metrics:
    """Score one ranking, rejecting unreviewed top-five predictions."""

    return _score_queries(benchmark.queries, _results_by_id(results))


def evaluate_benchmark(
    materials: Sequence[Mapping[str, str]],
    benchmark: Benchmark,
    *,
    weights: Sequence[float] = WEIGHT_GRID,
) -> EvaluationReport:
    """Compare fixed weights and measure input-order stability of the winner."""

    if not weights:
        raise ValueError("at least one candidate weight is required")
    if len(set(weights)) != len(weights):
        raise ValueError("candidate weights must be unique")

    evaluations: list[WeightEvaluation] = []
    selected_results: dict[float, tuple[RetrievalResult, ...]] = {}
    slice_names = sorted({name for query in benchmark.queries for name in query.slices})
    for weight in weights:
        results = rank_alternatives(materials, word_weight=weight)
        selected_results[weight] = results
        by_id = _results_by_id(results)
        evaluations.append(
            WeightEvaluation(
                word_weight=weight,
                character_weight=1.0 - weight,
                metrics=_score_queries(benchmark.queries, by_id),
                slices=tuple(
                    SliceMetrics(
                        name,
                        _score_queries(
                            tuple(
                                query
                                for query in benchmark.queries
                                if name in query.slices
                            ),
                            by_id,
                        ),
                    )
                    for name in slice_names
                ),
            )
        )

    selected = select_weight(evaluations)
    reversed_results = rank_alternatives(
        tuple(reversed(materials)), word_weight=selected.word_weight
    )
    stability = _stability(
        benchmark.queries,
        _results_by_id(selected_results[selected.word_weight]),
        _results_by_id(reversed_results),
    )
    return EvaluationReport(
        selected_word_weight=selected.word_weight,
        selected_character_weight=selected.character_weight,
        stability=stability,
        evaluations=tuple(evaluations),
    )


def evaluate_hybrid_benchmark(
    materials: Sequence[Mapping[str, str]],
    benchmark: Benchmark,
) -> HybridEvaluationReport:
    """Compare the prototype with the unchanged text default on reviewed labels."""

    text_results = rank_alternatives(materials)
    hybrid_results = _hybrid_retrieval_results(rank_hybrid_alternatives(materials))
    reversed_hybrid = _hybrid_retrieval_results(
        rank_hybrid_alternatives(tuple(reversed(materials)))
    )
    text_metrics = score_results(benchmark, text_results)
    hybrid_metrics = score_results(benchmark, hybrid_results)
    text_hard_negative_rate = _hard_negative_rate(
        benchmark.queries, _results_by_id(text_results)
    )
    hybrid_hard_negative_rate = _hard_negative_rate(
        benchmark.queries, _results_by_id(hybrid_results)
    )
    stability = _stability(
        benchmark.queries,
        _results_by_id(hybrid_results),
        _results_by_id(reversed_hybrid),
    )
    reasons: list[str] = []
    if hybrid_metrics.ndcg_at_5 <= text_metrics.ndcg_at_5:
        reasons.append("hybrid nDCG@5 did not improve")
    if hybrid_hard_negative_rate >= text_hard_negative_rate:
        reasons.append("hybrid hard-negative rate did not decrease")
    if hybrid_metrics.coverage < text_metrics.coverage:
        reasons.append("hybrid coverage regressed")
    if hybrid_metrics.expected_status_rate < text_metrics.expected_status_rate:
        reasons.append("hybrid expected-status agreement regressed")
    if stability != 1.0:
        reasons.append("hybrid results changed with input order")
    return HybridEvaluationReport(
        text_metrics=text_metrics,
        hybrid_metrics=hybrid_metrics,
        text_hard_negative_rate=text_hard_negative_rate,
        hybrid_hard_negative_rate=hybrid_hard_negative_rate,
        hybrid_stability=stability,
        promoted=not reasons,
        non_promotion_reasons=tuple(reasons),
    )


def select_weight(evaluations: Sequence[WeightEvaluation]) -> WeightEvaluation:
    """Choose usable retrieval first, then relevance and the neutral prior."""

    if not evaluations:
        raise ValueError("at least one weight evaluation is required")
    return min(
        evaluations,
        key=lambda item: (
            -item.metrics.expected_status_rate,
            -item.metrics.coverage,
            -item.metrics.ndcg_at_5,
            -item.metrics.precision_at_5,
            abs(item.word_weight - 0.5),
            item.word_weight,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Evaluate the reviewed benchmark and emit deterministic JSON."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("benchmark", type=Path)
    parser.add_argument(
        "--mode",
        choices=("text", "hybrid", "comparison"),
        default="text",
        help="Evaluate text weights, the prototype, or the versioned business report",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="Markdown destination required by comparison mode",
    )
    parser.add_argument(
        "--safety-benchmark",
        type=Path,
        help="Reviewed safety benchmark required by comparison mode",
    )
    args = parser.parse_args(argv)
    catalog_path = cast(Path, args.catalog)
    benchmark_path = cast(Path, args.benchmark)
    with catalog_path.open("rb") as handle:
        digest = file_digest(handle, "sha256").hexdigest()
    materials = load_materials(catalog_path)
    benchmark = load_benchmark(benchmark_path, materials, catalog_sha256=digest)
    if args.mode == "comparison":
        output = cast(Path | None, args.output)
        markdown_output = cast(Path | None, args.markdown_output)
        safety_path = cast(Path | None, args.safety_benchmark)
        if output is None or markdown_output is None or safety_path is None:
            parser.error(
                "comparison mode requires --output, --markdown-output, and --safety-benchmark"
            )
        report_object = build_comparison_report(
            materials,
            benchmark,
            load_safety_benchmark(safety_path),
            dataset_fingerprint=digest,
        )
        write_comparison_report(report_object, output, markdown_output)
        return 0
    report = (
        evaluate_hybrid_benchmark(materials, benchmark)
        if args.mode == "hybrid"
        else evaluate_benchmark(materials, benchmark)
    )
    rendered = (
        json.dumps(
            asdict(report),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    output = cast(Path | None, args.output)
    if output is None:
        print(rendered, end="")
    else:
        output.write_text(rendered, encoding="utf-8")
    return 0


def _business_as_retrieval(result: BusinessRetrievalResult) -> RetrievalResult:
    if result.mode != "strict_hybrid":
        raise BenchmarkError("relevance comparison accepts strict hybrid results only")
    status: RetrievalStatus = (
        "ok" if result.status == "ok" else "insufficient_candidates"
    )
    return RetrievalResult(
        result.part_id,
        status,
        tuple(alternative.text for alternative in result.alternatives),
    )


def _parser_summary(
    materials: Sequence[Mapping[str, str]], policy: CompatibilityPolicy
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    states: dict[str, Counter[str]] = {}
    failures: dict[str, Counter[str]] = {}
    for material in materials:
        for attribute in parse_material_attributes(material, policy=policy):
            states.setdefault(attribute.name, Counter())[attribute.state] += 1
            if attribute.reason is not None:
                failures.setdefault(attribute.name, Counter())[attribute.reason] += 1
    return (
        {name: dict(sorted(counts.items())) for name, counts in sorted(states.items())},
        {
            name: dict(sorted(counts.items()))
            for name, counts in sorted(failures.items())
        },
    )


def _status_rate(results: Sequence[BusinessRetrievalResult], status: str) -> float:
    return (
        round(sum(result.status == status for result in results) / len(results), 6)
        if results
        else 0.0
    )


def _installed_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "not-installed"


def _query(
    value: object,
    rows_by_id: Mapping[str, Mapping[str, str]],
    index: int,
) -> BenchmarkQuery:
    location = f"queries[{index}]"
    item = _object(value, location)
    part_id = _text(item.get("part_id"), f"{location}.part_id")
    if part_id not in rows_by_id:
        raise BenchmarkError(f"{location}.part_id is absent from the catalog")
    status_value = _text(item.get("expected_status"), f"{location}.expected_status")
    if status_value not in _EXPECTED_STATUSES:
        raise BenchmarkError(f"{location}.expected_status is invalid")
    expected_status = cast(RetrievalStatus, status_value)
    slices = tuple(
        _text(name, f"{location}.slices")
        for name in _array(item.get("slices"), f"{location}.slices")
    )
    if not slices or len(set(slices)) != len(slices):
        raise BenchmarkError(f"{location}.slices must be non-empty and unique")
    judgments = tuple(
        _judgment(judgment, location, rows_by_id, part_id, judgment_index)
        for judgment_index, judgment in enumerate(
            _array(item.get("judgments"), f"{location}.judgments")
        )
    )
    judgment_ids = [judgment.part_id for judgment in judgments]
    if len(set(judgment_ids)) != len(judgment_ids):
        raise BenchmarkError(f"{location} contains duplicate candidate judgments")
    has_description = bool(
        normalize_description(rows_by_id[part_id][DESCRIPTION_COLUMN])
    )
    if expected_status == "insufficient_description" and has_description:
        raise BenchmarkError(f"{location} expects abstention for usable text")
    if not has_description and expected_status != "insufficient_description":
        raise BenchmarkError(
            f"{location} expects a non-abstaining result without usable text"
        )
    if expected_status == "ok" and not judgments:
        raise BenchmarkError(f"{location} has no reviewed candidates")
    if expected_status == "insufficient_description" and judgments:
        raise BenchmarkError(f"{location} abstention must not invent judgments")
    return BenchmarkQuery(
        part_id=part_id,
        expected_status=expected_status,
        slices=slices,
        rationale=_text(item.get("rationale"), f"{location}.rationale"),
        judgments=judgments,
    )


def _judgment(
    value: object,
    query_location: str,
    rows_by_id: Mapping[str, Mapping[str, str]],
    query_id: str,
    index: int,
) -> Judgment:
    location = f"{query_location}.judgments[{index}]"
    item = _object(value, location)
    part_id = _text(item.get("part_id"), f"{location}.part_id")
    if part_id == query_id:
        raise BenchmarkError(f"{location} leaks the query into its candidate pool")
    if part_id not in rows_by_id:
        raise BenchmarkError(f"{location}.part_id is absent from the catalog")
    grade = _integer(item.get("grade"), f"{location}.grade")
    if not 0 <= grade <= 3:
        raise BenchmarkError(f"{location}.grade must be between 0 and 3")
    return Judgment(
        part_id=part_id,
        grade=grade,
        rationale=_text(item.get("rationale"), f"{location}.rationale"),
    )


def _score_queries(
    queries: Sequence[BenchmarkQuery],
    results_by_id: Mapping[str, RetrievalResult],
) -> Metrics:
    precisions: list[float] = []
    ndcgs: list[float] = []
    status_matches = 0
    for query in queries:
        result = results_by_id.get(query.part_id)
        if result is None:
            raise BenchmarkError(f"retrieval omitted benchmark query {query.part_id}")
        status_matches += result.status == query.expected_status
        candidate_ids = [candidate.part_id for candidate in result.alternatives]
        if result.status not in _EXPECTED_STATUSES:
            raise BenchmarkError(f"{query.part_id} has an invalid retrieval status")
        if result.status == "ok" and len(candidate_ids) != TOP_K:
            raise BenchmarkError(f"{query.part_id} did not return exactly five results")
        if result.status == "insufficient_candidates" and len(candidate_ids) >= TOP_K:
            raise BenchmarkError(
                f"{query.part_id} insufficient result returned five or more candidates"
            )
        if result.status == "insufficient_description" and candidate_ids:
            raise BenchmarkError(
                f"{query.part_id} description abstention returned alternatives"
            )
        if query.part_id in candidate_ids or len(set(candidate_ids)) != len(
            candidate_ids
        ):
            raise BenchmarkError(f"{query.part_id} ranking contains self or duplicates")
        grades_by_id = {
            judgment.part_id: judgment.grade for judgment in query.judgments
        }
        unreviewed = [
            candidate_id
            for candidate_id in candidate_ids
            if candidate_id not in grades_by_id
        ]
        if unreviewed:
            raise BenchmarkError(
                f"{query.part_id} has unreviewed candidates: {unreviewed}"
            )
        if result.status != "ok":
            continue
        grades = [grades_by_id[candidate_id] for candidate_id in candidate_ids]
        precisions.append(sum(grade >= RELEVANT_GRADE for grade in grades) / TOP_K)
        ndcgs.append(_ndcg(grades, [item.grade for item in query.judgments]))

    count = len(queries)
    ranked = len(precisions)
    return Metrics(
        precision_at_5=_mean(precisions),
        ndcg_at_5=_mean(ndcgs),
        coverage=round(ranked / count, 6),
        expected_status_rate=round(status_matches / count, 6),
        query_count=count,
        ranked_query_count=ranked,
    )


def _ndcg(ranked_grades: Sequence[int], all_grades: Sequence[int]) -> float:
    dcg = sum(
        (2**grade - 1) / log2(rank + 2)
        for rank, grade in enumerate(ranked_grades[:TOP_K])
    )
    ideal = sorted(all_grades, reverse=True)[:TOP_K]
    ideal_dcg = sum((2**grade - 1) / log2(rank + 2) for rank, grade in enumerate(ideal))
    return round(dcg / ideal_dcg, 6) if ideal_dcg else 0.0


def _stability(
    queries: Sequence[BenchmarkQuery],
    first: Mapping[str, RetrievalResult],
    second: Mapping[str, RetrievalResult],
) -> float:
    stable = sum(first[query.part_id] == second[query.part_id] for query in queries)
    return round(stable / len(queries), 6)


def _hybrid_retrieval_results(
    results: Sequence[HybridRetrievalResult],
) -> tuple[RetrievalResult, ...]:
    return tuple(
        RetrievalResult(
            part_id=result.part_id,
            status=result.status,
            alternatives=tuple(item.text for item in result.alternatives),
        )
        for result in results
    )


def _hard_negative_rate(
    queries: Sequence[BenchmarkQuery],
    results_by_id: Mapping[str, RetrievalResult],
) -> float:
    hard_negatives = 0
    returned = 0
    for query in queries:
        result = results_by_id[query.part_id]
        grades = {judgment.part_id: judgment.grade for judgment in query.judgments}
        for alternative in result.alternatives:
            hard_negatives += grades[alternative.part_id] == 0
            returned += 1
    return round(hard_negatives / returned, 6) if returned else 0.0


def _results_by_id(
    results: Sequence[RetrievalResult],
) -> dict[str, RetrievalResult]:
    by_id = {result.part_id: result for result in results}
    if len(by_id) != len(results):
        raise BenchmarkError("retrieval results contain duplicate part IDs")
    return by_id


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise BenchmarkError(f"{location} must be an object")
    return cast(dict[str, object], value)


def _array(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise BenchmarkError(f"{location} must be an array")
    return cast(list[object], value)


def _text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError(f"{location} must be non-blank text")
    return value.strip()


def _integer(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchmarkError(f"{location} must be an integer")
    return value


def _string_mapping(value: object, location: str) -> dict[str, str]:
    item = _object(value, location)
    if any(not isinstance(raw, str) for raw in item.values()):
        raise BenchmarkError(f"{location} values must be text")
    unexpected = sorted(set(item) - set(MATERIAL_COLUMNS))
    if unexpected:
        raise BenchmarkError(f"{location} has unsupported fields: {unexpected}")
    return {key: cast(str, raw) for key, raw in item.items()}


def _safety_case(
    value: object,
    defaults: Mapping[str, str],
    index: int,
) -> SafetyCase:
    location = f"cases[{index}]"
    item = _object(value, location)
    case_id = _text(item.get("case_id"), f"{location}.case_id")
    split = _text(item.get("split"), f"{location}.split")
    if split not in {"training", "held_out"}:
        raise BenchmarkError(f"{location}.split is invalid")
    expected_outcome = _text(
        item.get("expected_outcome"), f"{location}.expected_outcome"
    )
    if expected_outcome not in {"compatible", "conflict", "insufficient_evidence"}:
        raise BenchmarkError(f"{location}.expected_outcome is invalid")
    tags = tuple(
        _text(tag, f"{location}.tags")
        for tag in _array(item.get("tags"), f"{location}.tags")
    )
    if not tags or len(set(tags)) != len(tags):
        raise BenchmarkError(f"{location}.tags must be non-empty and unique")
    expected_codes = tuple(
        _text(code, f"{location}.expected_codes")
        for code in _array(item.get("expected_codes"), f"{location}.expected_codes")
    )
    if len(set(expected_codes)) != len(expected_codes):
        raise BenchmarkError(f"{location}.expected_codes must be unique")
    query = dict.fromkeys(MATERIAL_COLUMNS, "")
    query.update(defaults)
    query.update(_string_mapping(item.get("query"), f"{location}.query"))
    query[PART_ID_COLUMN] = f"{case_id}:query"
    candidate = dict.fromkeys(MATERIAL_COLUMNS, "")
    candidate.update(defaults)
    candidate.update(_string_mapping(item.get("candidate"), f"{location}.candidate"))
    candidate[PART_ID_COLUMN] = f"{case_id}:candidate"
    return SafetyCase(
        case_id=case_id,
        split=cast(Literal["training", "held_out"], split),
        rule=_text(item.get("rule"), f"{location}.rule"),
        tags=tags,
        query=query,
        candidate=candidate,
        expected_outcome=expected_outcome,
        expected_codes=expected_codes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
