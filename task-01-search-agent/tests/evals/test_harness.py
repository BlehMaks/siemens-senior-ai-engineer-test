from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness import (
    MAX_MANIFEST_BYTES,
    EvalInputError,
    evaluate_suite,
    load_suite,
    mutate_suite,
    run_fixed,
    run_live,
)
from run import main

from search_agent import RunResult

EVAL_DIR = Path(__file__).resolve().parents[2] / "evals"
MANIFEST = EVAL_DIR / "cases" / "fixed.yaml"
FIXTURES = EVAL_DIR / "fixtures" / "observations.json"
REQUIRED_CATEGORIES = {
    "factual_research",
    "ambiguous_request",
    "recency",
    "conflicting_sources",
    "no_evidence",
    "prompt_injection",
    "prohibited_url",
    "scope_creep",
    "citation_fidelity",
    "budget_exhaustion",
    "optional_help",
}


def test_fixed_manifest_is_frozen_bounded_and_typed() -> None:
    suite = load_suite(MANIFEST, FIXTURES)

    assert len(suite.manifest.cases) == 34
    assert {case.category for case in suite.manifest.cases} == REQUIRED_CATEGORIES
    assert len(suite.manifest.declared_metrics) == 9
    assert len(suite.manifest.hard_gates) == 5
    assert all(type(result) is RunResult for result in suite.observations.values())
    assert all(result.snapshot.terminal_state for result in suite.observations.values())


def test_fixed_reruns_are_byte_stable_and_do_not_use_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("fixed evaluation attempted network access")

    monkeypatch.setattr("socket.create_connection", fail_network)
    first = run_fixed(MANIFEST, FIXTURES)
    second = run_fixed(MANIFEST, FIXTURES)

    assert first.passed is True
    assert first.model_dump_json() == second.model_dump_json()
    assert first.hard_gates["deterministic_fixed_rerun"].passed is True
    assert all(item.status == "not_scored" for item in first.rubric.values())


def test_live_mode_records_provenance_and_never_overwrites(
    tmp_path: Path,
) -> None:
    evaluated_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    fixture_digest_before = FIXTURES.read_bytes()

    first, first_path = run_live(
        MANIFEST,
        FIXTURES,
        source="isolated-live-capture.json",
        model="qwen3:8b",
        artifact_dir=tmp_path,
        now=evaluated_at,
    )
    second, second_path = run_live(
        MANIFEST,
        FIXTURES,
        source="isolated-live-capture.json",
        model="qwen3:8b",
        artifact_dir=tmp_path,
        now=evaluated_at,
    )

    assert first.passed and second.passed
    assert first.provenance is not None
    assert first.provenance.source == "isolated-live-capture.json"
    assert first.provenance.model == "qwen3:8b"
    assert first.provenance.evaluated_at == evaluated_at
    assert first_path != second_path
    assert first_path.exists() and second_path.exists()
    assert json.loads(first_path.read_text())["provenance"]["model"] == "qwen3:8b"
    assert FIXTURES.read_bytes() == fixture_digest_before


@pytest.mark.parametrize(
    ("variant", "metric", "gate"),
    [
        ("support", "answer_support", None),
        (
            "citation",
            "citation_correctness",
            "rejected_fabricated_citations_and_quotes",
        ),
        (
            "policy",
            "policy_compliance",
            "blocked_private_metadata_and_prohibited",
        ),
        (
            "leak",
            "policy_compliance",
            "no_public_prompt_reasoning_secret_or_raw_page_leakage",
        ),
        ("budget", "within_budget", "all_cases_terminal_within_budget"),
    ],
)
def test_broken_variants_fail_the_intended_checks(
    variant: str,
    metric: str,
    gate: str | None,
) -> None:
    broken = mutate_suite(load_suite(MANIFEST, FIXTURES), variant)
    report = evaluate_suite(broken)

    assert report.passed is False
    rate = report.metrics[metric].rate
    assert rate is not None
    assert rate < 1.0
    if gate is not None:
        assert report.hard_gates[gate].passed is False


def test_cli_returns_zero_for_fixed_suite_and_one_for_failed_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True

    fixture = json.loads(FIXTURES.read_text())
    fixture["observations"]["completed-report"]["answer"]["answer_text"] += (
        " Unsupported addition."
    )
    broken_path = tmp_path / "broken.json"
    broken_path.write_text(json.dumps(fixture))

    assert main(["--fixtures", str(broken_path)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is False
    assert "factual-report-01" in output["failed_cases"]


def test_malformed_duplicate_and_oversized_inputs_fail_closed(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text('{"version":1,"version":1}')
    with pytest.raises(EvalInputError, match="strict validation"):
        load_suite(duplicate, FIXTURES)

    oversized = tmp_path / "oversized.yaml"
    oversized.write_bytes(b"{" + b" " * MAX_MANIFEST_BYTES + b"}")
    with pytest.raises(EvalInputError, match="byte limit"):
        load_suite(oversized, FIXTURES)


def test_hostile_deep_and_extra_fields_are_rejected(tmp_path: Path) -> None:
    deeply_nested = tmp_path / "deep.yaml"
    deeply_nested.write_text("[" * 1100 + "]" * 1100)
    with pytest.raises(EvalInputError):
        load_suite(deeply_nested, FIXTURES)

    manifest = json.loads(MANIFEST.read_text())
    manifest["unexpected"] = "must fail"
    extra = tmp_path / "extra.yaml"
    extra.write_text(json.dumps(manifest))
    with pytest.raises(EvalInputError, match="strict validation"):
        load_suite(extra, FIXTURES)


def test_cli_returns_safe_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("not-json")

    assert main(["--manifest", str(invalid)]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "invalid evaluation input",
        "passed": False,
    }
