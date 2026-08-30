from __future__ import annotations

import csv
import json
import os
import runpy
import sys
from dataclasses import replace
from hashlib import file_digest
from pathlib import Path

import pytest

import material_similarity.evaluation as evaluation_module
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
    build_comparison_report,
    evaluate_benchmark,
    evaluate_safety_benchmark,
    load_benchmark,
    load_safety_benchmark,
    score_results,
    select_weight,
    write_comparison_report,
)
from material_similarity.evaluation import (
    main as evaluation_main,
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
_FIXTURE_DIGEST = "0" * 64


def _material(part_id: str, description: str) -> dict[str, str]:
    material = dict.fromkeys(MATERIAL_COLUMNS, "")
    material[PART_ID_COLUMN] = part_id
    material[DESCRIPTION_COLUMN] = description
    return material


def _benchmark_payload() -> dict[str, object]:
    return {
        "version": 1,
        "catalog": {"sha256": _FIXTURE_DIGEST, "row_count": 6},
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
        load_benchmark(path, materials, catalog_sha256=_FIXTURE_DIGEST)
        .queries[0]
        .part_id
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
        load_benchmark(path, materials, catalog_sha256=_FIXTURE_DIGEST)

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
        load_benchmark(path, materials, catalog_sha256=_FIXTURE_DIGEST)

    payload = _benchmark_payload()
    queries = payload["queries"]
    assert isinstance(queries, list)
    queries.append(queries[0])
    _write_benchmark(path, payload)
    with pytest.raises(BenchmarkError, match="duplicate query IDs"):
        load_benchmark(path, materials, catalog_sha256=_FIXTURE_DIGEST)

    payload = _benchmark_payload()
    _write_benchmark(path, payload)
    with pytest.raises(BenchmarkError, match="SHA-256"):
        load_benchmark(path, materials, catalog_sha256="1" * 64)


@pytest.mark.parametrize(
    "digest",
    ["", "0" * 63, "0" * 65, "G" * 64, "A" * 64],
)
def test_benchmark_requires_a_lowercase_sha256(digest: str, tmp_path: Path) -> None:
    materials = tuple(_material(part_id, "shared fuse") for part_id in "QABCDE")
    path = tmp_path / "relevance.yaml"
    _write_benchmark(path, _benchmark_payload())

    with pytest.raises(BenchmarkError, match="64 lowercase hexadecimal"):
        load_benchmark(path, materials, catalog_sha256=digest)
    if not digest:
        with pytest.raises(BenchmarkError, match="64 lowercase hexadecimal"):
            load_benchmark(path, materials)


def test_benchmark_rejects_non_abstaining_status_for_blank_text(
    tmp_path: Path,
) -> None:
    materials = tuple(
        _material(part_id, "" if part_id == "Q" else "shared fuse")
        for part_id in "QABCDE"
    )
    payload = _benchmark_payload()
    queries = payload["queries"]
    assert isinstance(queries, list)
    query = queries[0]
    assert isinstance(query, dict)
    query["expected_status"] = "insufficient_candidates"
    path = tmp_path / "relevance.yaml"
    _write_benchmark(path, payload)

    with pytest.raises(
        BenchmarkError, match="non-abstaining result without usable text"
    ):
        load_benchmark(path, materials, catalog_sha256=_FIXTURE_DIGEST)


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


def test_weight_selection_prioritizes_expected_status_and_coverage() -> None:
    reliable = WeightEvaluation(
        0.5,
        0.5,
        Metrics(0.1, 0.1, 1.0, 1.0, 10, 10),
        (),
    )
    wrong_status = replace(
        reliable,
        word_weight=0.25,
        character_weight=0.75,
        metrics=Metrics(1.0, 1.0, 1.0, 0.9, 10, 10),
    )
    lower_coverage = replace(
        reliable,
        word_weight=0.75,
        character_weight=0.25,
        metrics=Metrics(1.0, 1.0, 0.9, 1.0, 10, 9),
    )

    assert select_weight((wrong_status, lower_coverage, reliable)) == reliable


@pytest.mark.parametrize(
    ("status", "candidate_ids", "message"),
    [
        ("ok", "ABCD", "exactly five"),
        ("insufficient_candidates", "ABCDE", "five or more"),
        ("insufficient_description", "A", "abstention returned alternatives"),
        ("insufficient_candidates", "Q", "self or duplicates"),
        ("insufficient_candidates", "AA", "self or duplicates"),
        ("insufficient_candidates", "Z", "unreviewed"),
        ("invalid", "", "invalid retrieval status"),
    ],
)
def test_metrics_validate_every_status_before_skipping_relevance(
    status: str,
    candidate_ids: str,
    message: str,
) -> None:
    query = BenchmarkQuery(
        "Q",
        "insufficient_candidates",
        ("fixture",),
        "fixture",
        tuple(Judgment(part_id, 2, "fixture") for part_id in "ABCDE"),
    )
    result = RetrievalResult(
        "Q",
        status,  # type: ignore[arg-type] - invalid runtime combinations are the target.
        tuple(_alternative(part_id) for part_id in candidate_ids),
    )

    with pytest.raises(BenchmarkError, match=message):
        score_results(Benchmark(_FIXTURE_DIGEST, 6, (query,)), (result,))


@pytest.mark.parametrize("weight", [-0.1, 1.1, float("nan"), float("inf")])
def test_retrieval_rejects_invalid_evaluation_weights(weight: float) -> None:
    materials = tuple(_material(part_id, "shared fuse") for part_id in "QABCDE")

    with pytest.raises(ValueError, match="word_weight"):
        rank_alternatives(materials, word_weight=weight)


def test_safety_benchmark_is_bounded_reviewed_and_deterministic() -> None:
    path = Path(__file__).parents[1] / "evals" / "safety.yaml"

    benchmark = load_safety_benchmark(path)
    first = evaluate_safety_benchmark(benchmark)
    second = evaluate_safety_benchmark(benchmark)

    assert first == second
    assert len(first) == 20
    assert all(result.passed for result in first)
    assert {result.split for result in first} == {"training", "held_out"}
    assert {result.rule for result in first} >= {
        "current",
        "ac_voltage",
        "dc_voltage",
        "dimensions",
        "acting",
        "material",
        "mounting",
        "mounting_feature",
    }


def test_safety_benchmark_detects_a_mutated_expected_label(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "evals" / "safety.yaml"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["cases"][1]["expected_outcome"] = "compatible"
    path = tmp_path / "safety.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")

    results = evaluate_safety_benchmark(load_safety_benchmark(path))

    assert results[1].passed is False
    assert results[1].actual_outcome == "conflict"


def test_comparison_report_renders_json_and_markdown_from_one_object(
    tmp_path: Path,
) -> None:
    materials = (
        *(_material(part_id, "shared fuse") for part_id in "QABCDEF"),
        _material("BLANK", ""),
    )
    benchmark = Benchmark(
        _FIXTURE_DIGEST,
        len(materials),
        (
            BenchmarkQuery(
                "Q",
                "ok",
                ("fixture",),
                "Reviewed synthetic ranking.",
                tuple(
                    Judgment(part_id, 2, "Reviewed synthetic candidate.")
                    for part_id in "ABCDE"
                ),
            ),
        ),
    )
    safety = load_safety_benchmark(Path(__file__).parents[1] / "evals" / "safety.yaml")

    report = build_comparison_report(
        materials,
        benchmark,
        safety,
        dataset_fingerprint=_FIXTURE_DIGEST,
        runtime_seconds=(0.0, 0.0),
    )
    json_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"
    write_comparison_report(report, json_path, markdown_path)

    assert json.loads(json_path.read_text(encoding="utf-8")) == report
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "| Lexical v1 | evaluated | 1 | 1.0 | 1.0 | 1.0 |" in markdown
    assert "| Relaxed hybrid | not_implemented |" in markdown
    extension = report["business_extension"]
    assert isinstance(extension, dict)
    assert extension["status_counts"] == {"insufficient_evidence": 1, "ok": 7}
    assert extension["safety_benchmark"]["passed_count"] == 20
    reports = Path(__file__).parents[1] / "reports"
    assert (
        json.loads((reports / "baseline-vs-extension.json").read_text(encoding="utf-8"))
        == report
    )
    assert (reports / "baseline-vs-extension.md").read_text(
        encoding="utf-8"
    ) == markdown


def test_comparison_cli_writes_both_report_views(tmp_path: Path) -> None:
    catalog = tmp_path / "Fuse.csv"
    with catalog.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MATERIAL_COLUMNS, delimiter=";")
        writer.writeheader()
        for part_id in "QABCDE":
            writer.writerow(_material(part_id, "shared fuse"))
    with catalog.open("rb") as handle:
        digest = file_digest(handle, "sha256").hexdigest()
    benchmark_payload = _benchmark_payload()
    benchmark_payload["catalog"] = {"sha256": digest, "row_count": 6}
    benchmark = tmp_path / "benchmark.yaml"
    _write_benchmark(benchmark, benchmark_payload)
    json_path = tmp_path / "comparison.json"
    markdown_path = tmp_path / "comparison.md"

    assert (
        evaluation_main(
            [
                str(catalog),
                str(benchmark),
                "--mode",
                "comparison",
                "--safety-benchmark",
                str(Path(__file__).parents[1] / "evals" / "safety.yaml"),
                "--output",
                str(json_path),
                "--markdown-output",
                str(markdown_path),
            ]
        )
        == 0
    )

    assert json.loads(json_path.read_text(encoding="utf-8"))["schema_version"] == "1.0"
    assert markdown_path.read_text(encoding="utf-8").startswith(
        "# Task 5 baseline versus business extension"
    )


def test_benchmark_loader_rejects_corrupt_version_rows_and_empty_queries(
    tmp_path: Path,
) -> None:
    materials = tuple(_material(part_id, "shared fuse") for part_id in "QABCDE")
    path = tmp_path / "benchmark.yaml"
    invalid_payloads = (
        ({**_benchmark_payload(), "version": 2}, "unsupported benchmark version"),
        (
            {
                **_benchmark_payload(),
                "catalog": {"sha256": _FIXTURE_DIGEST, "row_count": 5},
            },
            "row count",
        ),
        ({**_benchmark_payload(), "queries": []}, "has no queries"),
    )
    for payload, message in invalid_payloads:
        _write_benchmark(path, payload)
        with pytest.raises(BenchmarkError, match=message):
            load_benchmark(path, materials, catalog_sha256=_FIXTURE_DIGEST)

    path.write_text("{", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="cannot read benchmark"):
        load_benchmark(path, materials, catalog_sha256=_FIXTURE_DIGEST)
    with pytest.raises(BenchmarkError, match="cannot read benchmark"):
        load_benchmark(
            tmp_path / "absent.yaml", materials, catalog_sha256=_FIXTURE_DIGEST
        )


def test_safety_loader_rejects_release_gate_mutations(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "evals" / "safety.yaml"
    base = json.loads(source.read_text(encoding="utf-8"))
    mutations: list[tuple[dict[str, object], str]] = []

    wrong_schema = json.loads(json.dumps(base))
    wrong_schema["schema_version"] = "2.0"
    mutations.append((wrong_schema, "schema version"))
    unreviewed = json.loads(json.dumps(base))
    unreviewed["reviewer_status"] = "draft"
    mutations.append((unreviewed, "must be reviewed"))
    empty = json.loads(json.dumps(base))
    empty["cases"] = []
    mutations.append((empty, "between 1 and 24"))
    duplicate = json.loads(json.dumps(base))
    duplicate["cases"][1]["case_id"] = duplicate["cases"][0]["case_id"]
    mutations.append((duplicate, "duplicate case IDs"))
    one_split = json.loads(json.dumps(base))
    for case in one_split["cases"]:
        case["split"] = "training"
    mutations.append((one_split, "separate training and held-out"))
    no_negative = json.loads(json.dumps(base))
    no_negative["cases"] = [
        case
        for case in no_negative["cases"]
        if case["case_id"] not in {"current-conflict", "parser-failure"}
    ]
    mutations.append((no_negative, "negative cases for current"))
    missing_tag = json.loads(json.dumps(base))
    for case in missing_tag["cases"]:
        case["tags"] = [
            "renamed" if tag == "blank_description" else tag for tag in case["tags"]
        ]
    mutations.append((missing_tag, "lacks required cases"))

    for index, (payload, message) in enumerate(mutations):
        path = tmp_path / f"safety-{index}.yaml"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(BenchmarkError, match=message):
            load_safety_benchmark(path)

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="cannot read safety"):
        load_safety_benchmark(malformed)


def test_safety_case_and_json_boundaries_reject_ambiguous_values() -> None:
    defaults: dict[str, str] = {}
    base: dict[str, object] = {
        "case_id": "case",
        "split": "training",
        "rule": "current",
        "tags": ["tag"],
        "query": {},
        "candidate": {},
        "expected_outcome": "compatible",
        "expected_codes": [],
    }
    mutations = (
        ({**base, "split": "future"}, "split is invalid"),
        ({**base, "expected_outcome": "maybe"}, "expected_outcome is invalid"),
        ({**base, "tags": []}, "tags must be non-empty"),
        ({**base, "tags": ["x", "x"]}, "tags must be non-empty"),
        ({**base, "expected_codes": ["x", "x"]}, "expected_codes must be unique"),
    )
    for payload, message in mutations:
        with pytest.raises(BenchmarkError, match=message):
            evaluation_module._safety_case(payload, defaults, 0)

    with pytest.raises(BenchmarkError, match="values must be text"):
        evaluation_module._string_mapping({"Acting": 1}, "mapping")
    with pytest.raises(BenchmarkError, match="unsupported fields"):
        evaluation_module._string_mapping({"unknown": "x"}, "mapping")


@pytest.mark.parametrize(
    ("call", "message"),
    (
        (lambda: evaluation_module._object([], "value"), "must be an object"),
        (lambda: evaluation_module._object({1: "x"}, "value"), "must be an object"),
        (lambda: evaluation_module._array({}, "value"), "must be an array"),
        (lambda: evaluation_module._text(" ", "value"), "non-blank text"),
        (lambda: evaluation_module._integer(True, "value"), "must be an integer"),
    ),
)
def test_evaluation_json_primitives_fail_closed(call: object, message: str) -> None:
    with pytest.raises(BenchmarkError, match=message):
        call()  # type: ignore[operator]


def test_query_and_judgment_validation_covers_all_semantic_errors() -> None:
    rows = {part_id: _material(part_id, "shared fuse") for part_id in "QABCDE"}
    base = _benchmark_payload()["queries"][0]
    assert isinstance(base, dict)
    mutations = (
        ({**base, "part_id": "missing"}, "absent from the catalog"),
        ({**base, "expected_status": "invalid"}, "expected_status is invalid"),
        ({**base, "slices": []}, "slices must be non-empty"),
        (
            {**base, "expected_status": "insufficient_description"},
            "abstention for usable text",
        ),
        ({**base, "judgments": []}, "has no reviewed candidates"),
    )
    for payload, message in mutations:
        with pytest.raises(BenchmarkError, match=message):
            evaluation_module._query(payload, rows, 0)

    blank_rows = {**rows, "BLANK": _material("BLANK", "")}
    blank = {
        **base,
        "part_id": "BLANK",
        "expected_status": "insufficient_description",
    }
    with pytest.raises(BenchmarkError, match="must not invent judgments"):
        evaluation_module._query(blank, blank_rows, 0)

    judgment = {"part_id": "missing", "grade": 2, "rationale": "review"}
    with pytest.raises(BenchmarkError, match="absent from the catalog"):
        evaluation_module._judgment(judgment, "queries[0]", rows, "Q", 0)
    with pytest.raises(BenchmarkError, match="between 0 and 3"):
        evaluation_module._judgment(
            {"part_id": "A", "grade": 4, "rationale": "review"},
            "queries[0]",
            rows,
            "Q",
            0,
        )


def test_report_and_evaluation_configuration_fail_closed(tmp_path: Path) -> None:
    materials = tuple(_material(part_id, "shared fuse") for part_id in "QABCDE")
    benchmark = Benchmark(
        _FIXTURE_DIGEST,
        6,
        (
            BenchmarkQuery(
                "Q",
                "ok",
                ("fixture",),
                "fixture",
                tuple(Judgment(part_id, 2, "fixture") for part_id in "ABCDE"),
            ),
        ),
    )
    safety = load_safety_benchmark(Path(__file__).parents[1] / "evals" / "safety.yaml")
    with pytest.raises(ValueError, match="fingerprint"):
        build_comparison_report(materials, benchmark, safety, dataset_fingerprint="bad")
    with pytest.raises(ValueError, match="runtime override"):
        build_comparison_report(
            materials,
            benchmark,
            safety,
            dataset_fingerprint=_FIXTURE_DIGEST,
            runtime_seconds=(-1.0, 0.0),
        )
    blank_materials = tuple(_material(part_id, "") for part_id in "QABCDE")
    blank_benchmark = replace(
        benchmark,
        queries=(
            replace(
                benchmark.queries[0],
                expected_status="insufficient_description",
                judgments=(),
            ),
        ),
    )
    with pytest.raises(BenchmarkError, match="no non-blank"):
        build_comparison_report(
            blank_materials,
            blank_benchmark,
            safety,
            dataset_fingerprint=_FIXTURE_DIGEST,
        )

    with pytest.raises(ValueError, match="at least one candidate weight"):
        evaluate_benchmark(materials, benchmark, weights=())
    with pytest.raises(ValueError, match="unique"):
        evaluate_benchmark(materials, benchmark, weights=(0.5, 0.5))
    with pytest.raises(ValueError, match="weight evaluation"):
        select_weight(())


def test_comparison_helpers_cover_review_and_missing_package_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    structured = evaluation_module.BusinessRetrievalResult(
        "2.0", "Q", "structured_only", "insufficient_evidence", (), (), 0.0, "none"
    )
    with pytest.raises(BenchmarkError, match="strict hybrid"):
        evaluation_module._business_as_retrieval(structured)

    material = _material("Q", "fuse")
    material["Current Rating"] = "bad"
    _, failures = evaluation_module._parser_summary(
        (material,), evaluation_module.DEFAULT_COMPATIBILITY_POLICY
    )
    assert failures["current"] == {"quantity has unsupported syntax": 1}

    def missing(_package: str) -> str:
        raise evaluation_module.PackageNotFoundError

    monkeypatch.setattr(evaluation_module, "version", missing)
    assert evaluation_module._installed_version("missing") == "not-installed"
    assert evaluation_module._status_rate((), "ok") == 0.0


def test_metrics_reject_missing_and_duplicate_results() -> None:
    query = BenchmarkQuery("Q", "insufficient_candidates", ("fixture",), "fixture", ())
    with pytest.raises(BenchmarkError, match="omitted"):
        score_results(Benchmark(_FIXTURE_DIGEST, 1, (query,)), ())
    duplicate = RetrievalResult("Q", "insufficient_candidates", ())
    with pytest.raises(BenchmarkError, match="duplicate part IDs"):
        evaluation_module._results_by_id((duplicate, duplicate))


def test_evaluation_cli_covers_text_hybrid_and_missing_comparison_outputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = tmp_path / "Fuse.csv"
    with catalog.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MATERIAL_COLUMNS, delimiter=";")
        writer.writeheader()
        for part_id in "QABCDE":
            writer.writerow(_material(part_id, "shared fuse"))
    with catalog.open("rb") as handle:
        digest = file_digest(handle, "sha256").hexdigest()
    payload = _benchmark_payload()
    payload["catalog"] = {"sha256": digest, "row_count": 6}
    benchmark = tmp_path / "benchmark.yaml"
    _write_benchmark(benchmark, payload)

    assert evaluation_main([str(catalog), str(benchmark)]) == 0
    assert json.loads(capsys.readouterr().out)["selected_word_weight"] == 0.5

    hybrid_output = tmp_path / "hybrid.json"
    assert (
        evaluation_main(
            [
                str(catalog),
                str(benchmark),
                "--mode",
                "hybrid",
                "--output",
                str(hybrid_output),
            ]
        )
        == 0
    )
    assert "promoted" in json.loads(hybrid_output.read_text(encoding="utf-8"))

    with pytest.raises(SystemExit, match="2"):
        evaluation_main([str(catalog), str(benchmark), "--mode", "comparison"])


def test_evaluation_module_entry_point_runs_the_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = tmp_path / "Fuse.csv"
    with catalog.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MATERIAL_COLUMNS, delimiter=";")
        writer.writeheader()
        for part_id in "QABCDE":
            writer.writerow(_material(part_id, "shared fuse"))
    with catalog.open("rb") as handle:
        digest = file_digest(handle, "sha256").hexdigest()
    payload = _benchmark_payload()
    payload["catalog"] = {"sha256": digest, "row_count": 6}
    benchmark = tmp_path / "benchmark.yaml"
    _write_benchmark(benchmark, payload)
    monkeypatch.setattr(
        sys,
        "argv",
        ["material_similarity.evaluation", str(catalog), str(benchmark)],
    )

    with pytest.raises(SystemExit) as error:
        runpy.run_path(evaluation_module.__file__, run_name="__main__")

    assert error.value.code == 0
    assert json.loads(capsys.readouterr().out)["selected_word_weight"] == 0.5


def test_hybrid_promotion_gate_reports_each_independent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materials = tuple(_material(part_id, "shared fuse") for part_id in "QABCDE")
    benchmark = Benchmark(
        _FIXTURE_DIGEST,
        6,
        (
            BenchmarkQuery(
                "Q",
                "ok",
                ("fixture",),
                "fixture",
                tuple(Judgment(part_id, 2, "fixture") for part_id in "ABCDE"),
            ),
        ),
    )
    low = Metrics(0.2, 0.2, 0.5, 0.5, 1, 1)
    high = Metrics(0.8, 0.8, 1.0, 1.0, 1, 1)
    scores = iter((low, high))
    hard_negatives = iter((0.0, 0.0))
    monkeypatch.setattr(evaluation_module, "score_results", lambda *_args: next(scores))
    monkeypatch.setattr(
        evaluation_module,
        "_hard_negative_rate",
        lambda *_args: next(hard_negatives),
    )
    monkeypatch.setattr(evaluation_module, "_stability", lambda *_args: 0.5)

    report = evaluation_module.evaluate_hybrid_benchmark(materials, benchmark)

    assert report.non_promotion_reasons == (
        "hybrid hard-negative rate did not decrease",
        "hybrid results changed with input order",
    )


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
