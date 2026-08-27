"""Deterministic evaluation of reviewed material-relevance judgments."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import file_digest
from math import log2
from pathlib import Path
from typing import cast

from material_similarity.data import (
    DESCRIPTION_COLUMN,
    PART_ID_COLUMN,
    load_materials,
    profile_materials,
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
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    catalog_path = cast(Path, args.catalog)
    benchmark_path = cast(Path, args.benchmark)
    with catalog_path.open("rb") as handle:
        digest = file_digest(handle, "sha256").hexdigest()
    materials = load_materials(catalog_path)
    benchmark = load_benchmark(benchmark_path, materials, catalog_sha256=digest)
    rendered = (
        json.dumps(
            asdict(evaluate_benchmark(materials, benchmark)),
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
    if expected_status == "ok" and not has_description:
        raise BenchmarkError(f"{location} expects ranking for blank text")
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


if __name__ == "__main__":  # pragma: no cover - exercised through ``main`` tests
    raise SystemExit(main())
