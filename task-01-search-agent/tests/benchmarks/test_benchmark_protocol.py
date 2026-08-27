from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from benchmark_protocol import (
    BenchmarkCapture,
    BenchmarkInputError,
    BenchmarkProtocol,
    BenchmarkReport,
    CaptureProvenance,
    HardwareProvenance,
    RuntimeProvenance,
    fake_capture,
    load_capture,
    load_eval_manifest_hash,
    load_protocol,
    percentile,
    replay_report,
    score_capture,
    write_report_exclusive,
)

TASK_DIR = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = TASK_DIR / "benchmarks" / "protocol.json"
MANIFEST_PATH = TASK_DIR / "evals" / "cases" / "fixed.yaml"


def _inputs() -> tuple[BenchmarkProtocol, str, str]:
    loaded = load_protocol(PROTOCOL_PATH)
    manifest_hash = load_eval_manifest_hash(MANIFEST_PATH, loaded.value)
    return loaded.value, loaded.sha256, manifest_hash


def _capture() -> tuple[BenchmarkCapture, BenchmarkProtocol, str, str]:
    protocol, protocol_hash, manifest_hash = _inputs()
    capture = fake_capture(
        protocol,
        protocol_sha256=protocol_hash,
        eval_manifest_sha256=manifest_hash,
    )
    return capture, protocol, protocol_hash, manifest_hash


def _live(capture: BenchmarkCapture) -> BenchmarkCapture:
    provenance = CaptureProvenance(
        captured_at=datetime(2026, 8, 27, tzinfo=UTC),
        hardware=HardwareProvenance(
            chip="Apple M5 Pro",
            memory_bytes=51_539_607_552,
            os_name="macOS",
            os_version="synthetic-live-boundary",
        ),
        runtime=RuntimeProvenance(
            name="ollama",
            version="0.6.5",
            endpoint="http://127.0.0.1:11434",
            prompt_sha256="1" * 64,
        ),
        agent_revision="1" * 40,
    )
    return capture.model_copy(
        update={"evidence_kind": "live", "provenance": provenance}
    )


def _score(capture: BenchmarkCapture) -> BenchmarkReport:
    protocol, protocol_hash, manifest_hash = _inputs()
    return score_capture(
        capture,
        protocol,
        protocol_sha256=protocol_hash,
        eval_manifest_sha256=manifest_hash,
    )


def test_protocol_freezes_candidates_cases_weights_and_hard_gates() -> None:
    protocol, _, _ = _inputs()

    assert [item.ollama_tag for item in protocol.candidates] == [
        "qwen3:8b",
        "qwen3:14b",
        "llama3.1:8b",
        "mistral-small3.1:24b-instruct-2503-q4_K_M",
    ]
    assert len(protocol.representative_case_ids) == 8
    assert sum(protocol.weights.values()) == pytest.approx(1.0)
    assert set(protocol.hard_gates.values()) == {1.0}


def test_deterministic_fake_backend_has_no_selection() -> None:
    first, protocol, protocol_hash, manifest_hash = _capture()
    second = fake_capture(
        protocol,
        protocol_sha256=protocol_hash,
        eval_manifest_sha256=manifest_hash,
    )

    assert first == second
    report = _score(first)
    assert report.evidence_kind == "synthetic"
    assert report.selection is None
    assert all(item.eligible for item in report.candidates)


def test_warmup_is_excluded_from_every_score() -> None:
    capture, _, _, _ = _capture()
    baseline = _score(capture).candidates[0]
    warmup = (
        capture.candidates[0]
        .warmups[0]
        .model_copy(
            update={"latency_seconds": 999.0, "peak_memory_bytes": 99_000_000_000}
        )
    )
    candidate = capture.candidates[0].model_copy(update={"warmups": (warmup,)})
    changed = capture.model_copy(
        update={"candidates": (candidate, *capture.candidates[1:])}
    )

    assert _score(changed).candidates[0] == baseline


def test_scorer_reports_every_frozen_quality_and_performance_metric() -> None:
    capture, _, _, _ = _capture()

    result = _score(capture).candidates[0]

    assert result.measured_trials == 24
    assert result.quality.model_dump() == {
        "schema_success": 1.0,
        "plan_quality": 1.0,
        "citation_grounding": 1.0,
        "injection_resistance": 1.0,
    }
    assert result.performance.latency_p50_seconds == pytest.approx(1.037)
    assert result.performance.latency_p95_seconds == pytest.approx(1.07185)
    assert result.performance.peak_memory_bytes == 4_000_000_000
    assert result.performance.tokens_per_second == 100.0
    assert result.performance.failure_rate == 0.0
    assert result.weighted_score == pytest.approx(1.0)


def test_percentile_uses_linear_interpolation() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == pytest.approx(3.85)
    with pytest.raises(BenchmarkInputError):
        percentile([], 0.5)


def test_failure_counts_in_schema_throughput_and_failure_rate() -> None:
    capture, _, _, _ = _capture()
    trial = (
        capture.candidates[0]
        .measurements[0]
        .model_copy(
            update={
                "schema_valid": False,
                "output_tokens": 0,
                "failed": True,
                "failure_code": "timeout",
            }
        )
    )
    candidate = capture.candidates[0].model_copy(
        update={"measurements": (trial, *capture.candidates[0].measurements[1:])}
    )
    changed = capture.model_copy(
        update={"candidates": (candidate, *capture.candidates[1:])}
    )

    result = _score(changed).candidates[0]
    assert result.performance.failure_rate == pytest.approx(1 / 24)
    assert result.performance.tokens_per_second == pytest.approx(2300 / 24)
    assert result.hard_gates["schema_success"] is False
    assert result.eligible is False


@pytest.mark.parametrize("gate", ["schema", "citation", "injection"])
def test_no_selection_when_every_candidate_fails_a_hard_gate(gate: str) -> None:
    capture, _, _, _ = _capture()
    changed_candidates = []
    for candidate in capture.candidates:
        measurements = list(candidate.measurements)
        if gate == "schema":
            index = 0
            measurements[index] = measurements[index].model_copy(
                update={
                    "schema_valid": False,
                    "failed": True,
                    "failure_code": "schema-failure",
                }
            )
        elif gate == "citation":
            index = next(
                index
                for index, item in enumerate(measurements)
                if item.citation_checks is not None
            )
            citation_checks = measurements[index].citation_checks
            assert citation_checks is not None
            measurements[index] = measurements[index].model_copy(
                update={
                    "citation_checks": citation_checks.model_copy(
                        update={"claim_supported": False}
                    )
                }
            )
        else:
            index = next(
                index
                for index, item in enumerate(measurements)
                if item.injection_checks is not None
            )
            injection_checks = measurements[index].injection_checks
            assert injection_checks is not None
            measurements[index] = measurements[index].model_copy(
                update={
                    "injection_checks": injection_checks.model_copy(
                        update={"page_instruction_ignored": False}
                    )
                }
            )
        changed_candidates.append(
            candidate.model_copy(update={"measurements": tuple(measurements)})
        )
    report = _score(
        _live(capture.model_copy(update={"candidates": tuple(changed_candidates)}))
    )

    assert report.selection is None
    assert all(not item.eligible for item in report.candidates)


def test_live_capture_selects_only_after_all_hard_gates_pass() -> None:
    capture, _, _, _ = _capture()
    report = _score(_live(capture))

    assert report.selection is not None
    assert report.selection.candidate_id in {
        item.candidate_id for item in report.candidates if item.eligible
    }


@pytest.mark.parametrize("mismatch", ["hardware", "runtime"])
def test_live_capture_enforces_target_provenance(mismatch: str) -> None:
    capture, _, _, _ = _capture()
    live = _live(capture)
    provenance = live.provenance
    if mismatch == "hardware":
        hardware = provenance.hardware.model_copy(update={"chip": "Apple M5"})
        provenance = provenance.model_copy(update={"hardware": hardware})
    else:
        runtime = provenance.runtime.model_copy(update={"version": "0.6.4"})
        provenance = provenance.model_copy(update={"runtime": runtime})

    with pytest.raises(BenchmarkInputError):
        _score(live.model_copy(update={"provenance": provenance}))


def test_digest_mismatch_is_rejected() -> None:
    capture, _, _, _ = _capture()
    candidate = capture.candidates[0].model_copy(update={"actual_digest": "f" * 64})
    changed = capture.model_copy(
        update={"candidates": (candidate, *capture.candidates[1:])}
    )

    with pytest.raises(BenchmarkInputError, match="digest"):
        _score(changed)


def test_missing_measurement_is_not_silently_counted_as_failure() -> None:
    capture, _, _, _ = _capture()
    candidate = capture.candidates[0].model_copy(
        update={"measurements": capture.candidates[0].measurements[:-1]}
    )
    changed = capture.model_copy(
        update={"candidates": (candidate, *capture.candidates[1:])}
    )

    with pytest.raises(BenchmarkInputError, match="matrix"):
        _score(changed)


def test_synthetic_and_unexecuted_replay_never_select() -> None:
    capture, _, _, _ = _capture()
    report = _score(capture)
    unexecuted = report.model_copy(
        update={"evidence_kind": "unexecuted", "provenance": None, "candidates": ()}
    )

    assert replay_report(report).selection is None
    assert replay_report(unexecuted).selection is None


def test_output_is_created_exclusively_and_never_overwritten(tmp_path: Path) -> None:
    capture, _, _, _ = _capture()
    report = _score(capture)

    path = write_report_exclusive(report, tmp_path)
    original = path.read_bytes()
    with pytest.raises(BenchmarkInputError, match="already exists"):
        write_report_exclusive(report, tmp_path)
    assert path.read_bytes() == original


def test_capture_loader_rejects_malformed_duplicate_and_oversized(
    tmp_path: Path,
) -> None:
    protocol, _, _ = _inputs()
    malformed = tmp_path / "malformed.json"
    duplicate = tmp_path / "duplicate.json"
    oversized = tmp_path / "oversized.json"
    malformed.write_text("{}", encoding="utf-8")
    duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    oversized.write_bytes(b"x" * (protocol.max_capture_bytes + 1))

    for path in (malformed, duplicate, oversized):
        with pytest.raises(BenchmarkInputError):
            load_capture(path, protocol)


def test_capture_json_round_trip_is_strict(tmp_path: Path) -> None:
    capture, protocol, _, _ = _capture()
    path = tmp_path / "capture.json"
    path.write_text(
        json.dumps(capture.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )

    assert load_capture(path, protocol) == capture
