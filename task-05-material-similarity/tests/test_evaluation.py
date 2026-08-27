from __future__ import annotations

import json
import os
from dataclasses import replace
from hashlib import file_digest
from pathlib import Path

import pytest

from material_similarity.data import (
    DESCRIPTION_COLUMN,
    MATERIAL_COLUMNS,
    PART_ID_COLUMN,
    load_materials,
)
from material_similarity.evaluation import (
    Benchmark,
    BenchmarkError,
    BenchmarkQuery,
    Judgment,
    Metrics,
    WeightEvaluation,
    evaluate_benchmark,
    load_benchmark,
    score_results,
    select_weight,
)
from material_similarity.retrieval import (
    WORD_WEIGHT,
    Alternative,
    RetrievalResult,
    rank_alternatives,
)

_DEFAULT_CATALOG = Path(__file__).parents[2] / "input" / "IT DA AI Tasks" / "Fuse.csv"
_CATALOG = Path(os.environ.get("SIEMENS_FUSE_CSV", _DEFAULT_CATALOG))
_BENCHMARK = Path(__file__).parents[1] / "evals" / "relevance.yaml"


def _material(part_id: str, description: str) -> dict[str, str]:
    material = dict.fromkeys(MATERIAL_COLUMNS, "")
    material[PART_ID_COLUMN] = part_id
    material[DESCRIPTION_COLUMN] = description
    return material


def _benchmark_payload() -> dict[str, object]:
    return {
        "version": 1,
        "catalog": {"sha256": "fixture", "row_count": 6},
        "queries": [
            {
                "part_id": "Q",
                "expected_status": "ok",
                "slices": ["fixture"],
                "rationale": "A reviewable fixture query.",
                "judgments": [
                    {
                        "part_id": candidate,
                        "grade": grade,
                        "rationale": "A reviewable fixture judgment.",
                    }
                    for candidate, grade in zip("ABCDE", (3, 2, 1, 0, 0), strict=True)
                ],
            }
        ],
    }


def _write_benchmark(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _alternative(part_id: str) -> Alternative:
    return Alternative(part_id, 1.0, 1.0, 1.0, ("shared",), ())


def test_benchmark_validation_blocks_self_duplicates_and_catalog_drift(
    tmp_path: Path,
) -> None:
    materials = tuple(_material(part_id, "shared fuse") for part_id in "QABCDE")
    path = tmp_path / "relevance.yaml"
    payload = _benchmark_payload()
    _write_benchmark(path, payload)
    assert (
        load_benchmark(path, materials, catalog_sha256="fixture").queries[0].part_id
        == "Q"
    )

    queries = payload["queries"]
    assert isinstance(queries, list)
    query = queries[0]
    assert isinstance(query, dict)
    judgments = query["judgments"]
    assert isinstance(judgments, list)
    judgments.append({"part_id": "Q", "grade": 3, "rationale": "self"})
    _write_benchmark(path, payload)
    with pytest.raises(BenchmarkError, match="leaks the query"):
        load_benchmark(path, materials)

    payload = _benchmark_payload()
    queries = payload["queries"]
    assert isinstance(queries, list)
    query = queries[0]
    assert isinstance(query, dict)
    judgments = query["judgments"]
    assert isinstance(judgments, list)
    judgments.append(judgments[0])
    _write_benchmark(path, payload)
    with pytest.raises(BenchmarkError, match="duplicate candidate judgments"):
        load_benchmark(path, materials)

    payload = _benchmark_payload()
    queries = payload["queries"]
    assert isinstance(queries, list)
    queries.append(queries[0])
    _write_benchmark(path, payload)
    with pytest.raises(BenchmarkError, match="duplicate query IDs"):
        load_benchmark(path, materials)

    payload = _benchmark_payload()
    _write_benchmark(path, payload)
    with pytest.raises(BenchmarkError, match="SHA-256"):
        load_benchmark(path, materials, catalog_sha256="changed")


def test_metrics_detect_a_deliberately_permuted_ranking() -> None:
    query = BenchmarkQuery(
        part_id="Q",
        expected_status="ok",
        slices=("fixture",),
        rationale="fixture",
        judgments=tuple(
            Judgment(part_id, grade, "fixture")
            for part_id, grade in zip("ABCDE", (3, 2, 1, 0, 0), strict=True)
        ),
    )
    blank = BenchmarkQuery(
        part_id="BLANK",
        expected_status="insufficient_description",
        slices=("blank",),
        rationale="fixture",
        judgments=(),
    )
    benchmark = Benchmark("fixture", 7, (query, blank))
    expected = (
        RetrievalResult("Q", "ok", tuple(_alternative(part_id) for part_id in "ABCDE")),
        RetrievalResult("BLANK", "insufficient_description", ()),
    )
    permuted = (
        replace(expected[0], alternatives=tuple(reversed(expected[0].alternatives))),
        expected[1],
    )

    metrics = score_results(benchmark, expected)
    degraded = score_results(benchmark, permuted)

    assert metrics == Metrics(0.4, 1.0, 0.5, 1.0, 2, 1)
    assert degraded.precision_at_5 == metrics.precision_at_5
    assert degraded.ndcg_at_5 < metrics.ndcg_at_5


def test_weight_selection_prefers_graded_relevance_then_the_neutral_prior() -> None:
    base = Metrics(0.6, 0.8, 1.0, 1.0, 1, 1)
    evaluations = tuple(
        WeightEvaluation(weight, 1.0 - weight, base, ()) for weight in (0.25, 0.5, 0.75)
    )
    assert select_weight(evaluations).word_weight == 0.5

    improved = replace(evaluations[0], metrics=replace(base, ndcg_at_5=0.81))
    assert select_weight((improved, *evaluations[1:])).word_weight == 0.25


@pytest.mark.parametrize("weight", [-0.1, 1.1, float("nan"), float("inf")])
def test_retrieval_rejects_invalid_evaluation_weights(weight: float) -> None:
    materials = tuple(_material(part_id, "shared fuse") for part_id in "QABCDE")

    with pytest.raises(ValueError, match="word_weight"):
        rank_alternatives(materials, word_weight=weight)


@pytest.mark.skipif(not _CATALOG.is_file(), reason="employer Fuse.csv is not public")
def test_reviewed_benchmark_runs_against_the_exact_fuse_catalog() -> None:
    with _CATALOG.open("rb") as handle:
        digest = file_digest(handle, "sha256").hexdigest()
    materials = load_materials(_CATALOG)
    benchmark = load_benchmark(_BENCHMARK, materials, catalog_sha256=digest)

    report = evaluate_benchmark(materials, benchmark)

    assert report.selected_word_weight == WORD_WEIGHT == 0.25
    assert report.selected_character_weight == 0.75
    assert report.stability == 1.0
    assert [trial.word_weight for trial in report.evaluations] == [
        0.0,
        0.25,
        0.5,
        0.75,
        1.0,
    ]
    assert all(trial.metrics.coverage == 0.875 for trial in report.evaluations)
    assert all(
        trial.metrics.expected_status_rate == 1.0 for trial in report.evaluations
    )
    assert [
        (
            trial.word_weight,
            trial.metrics.precision_at_5,
            trial.metrics.ndcg_at_5,
        )
        for trial in report.evaluations
    ] == [
        (0.0, 0.542857, 0.802445),
        (0.25, 0.542857, 0.846792),
        (0.5, 0.542857, 0.833304),
        (0.75, 0.542857, 0.761708),
        (1.0, 0.485714, 0.672381),
    ]
