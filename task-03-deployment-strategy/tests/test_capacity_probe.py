from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from deployment_strategy import capacity_probe
from deployment_strategy.capacity_probe import (
    ProbeConfig,
    ProbeThresholds,
    build_design_envelopes,
    main,
    run_probe,
)


@pytest.mark.asyncio
async def test_capacity_probe_exercises_required_local_paths() -> None:
    result = await run_probe(
        ProbeConfig(
            submissions=12,
            max_queued_runs=8,
            thresholds=ProbeThresholds(
                p95_submit_ms=1_000,
                p95_first_event_ms=1_000,
                recovery_ms=1_500,
            ),
        )
    )

    assert result["schema_version"] == 1
    assert result["assertions"]["passed"] is True
    assert result["measurements"]["accepted"] == 8
    assert result["measurements"]["rejected"] == 4
    assert result["measurements"]["duplicate_status_code"] == 202
    assert result["measurements"]["duplicate_run_id_matches"] is True
    assert result["measurements"]["first_event_samples"] == 8
    assert result["measurements"]["recovery_accepted"] is True
    assert result["measurements"]["recovery_terminal_state"] == "completed"
    assert result["measurements"]["recovery_terminal_success"] is True
    assert result["measurements"]["terminal"] == {
        "budget_exhausted": 1,
        "cancelled": 1,
        "completed": 6,
    }
    assert result["measurements"]["p95_submit_ms"] <= 1_000
    assert result["measurements"]["p95_first_event_ms"] <= 1_000
    assert result["measurements"]["queue_oldest_age_ms"] >= 0


@pytest.mark.asyncio
async def test_capacity_probe_respects_submission_count_below_queue_limit() -> None:
    result = await run_probe(
        ProbeConfig(
            submissions=4,
            max_queued_runs=8,
            cancelled_index=1,
            model_quota_index=2,
            thresholds=ProbeThresholds(
                p95_submit_ms=1_000,
                p95_first_event_ms=1_000,
                recovery_ms=1_500,
            ),
        )
    )

    assert result["assertions"]["passed"] is True
    assert result["scenario"]["submissions"] == 4
    assert result["measurements"]["accepted"] == 4
    assert result["measurements"]["rejected"] == 0
    assert result["measurements"]["terminal"] == {
        "budget_exhausted": 1,
        "cancelled": 1,
        "completed": 2,
    }


@pytest.mark.asyncio
async def test_recovery_threshold_includes_recovery_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_drain = capacity_probe._drain
    calls = 0

    async def delayed_recovery_drain(database_path: Path) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            await asyncio.sleep(0.1)
        return await original_drain(database_path)

    monkeypatch.setattr(capacity_probe, "_drain", delayed_recovery_drain)

    result = await run_probe(
        ProbeConfig(
            thresholds=ProbeThresholds(
                p95_submit_ms=1_000,
                p95_first_event_ms=1_000,
                recovery_ms=50,
            )
        )
    )

    assert result["measurements"]["recovery_terminal_success"] is True
    assert result["assertions"]["recovery_within_threshold"] is False


@pytest.mark.asyncio
async def test_first_event_latency_ignores_later_recovery_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_drain = capacity_probe._drain
    calls = 0

    async def delayed_recovery_drain(database_path: Path) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            await asyncio.sleep(0.3)
        return await original_drain(database_path)

    monkeypatch.setattr(capacity_probe, "_drain", delayed_recovery_drain)

    result = await run_probe(
        ProbeConfig(
            thresholds=ProbeThresholds(
                p95_submit_ms=1_000,
                p95_first_event_ms=1_000,
                recovery_ms=1_000,
            )
        )
    )

    assert result["assertions"]["sse_p95_within_threshold"] is True
    assert result["measurements"]["recovery_ms"] >= 300


@pytest.mark.asyncio
async def test_queue_age_starts_after_transport_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_handle = capacity_probe.ASGITransport.handle_async_request
    original_queue_age = capacity_probe._oldest_queue_age_ms
    observed_first_api_arrival: float | None = None
    age_observed_at: float | None = None
    delayed_keys: set[str] = set()

    async def delayed_transport(self, request):
        nonlocal observed_first_api_arrival
        key = request.headers.get("Idempotency-Key", "")
        if (
            request.method == "POST"
            and request.url.path.endswith("/runs")
            and key.startswith("capacity-")
            and key not in delayed_keys
        ):
            delayed_keys.add(key)
            await asyncio.sleep(0.1)
            now = time.perf_counter()
            if observed_first_api_arrival is None or now < observed_first_api_arrival:
                observed_first_api_arrival = now
        return await original_handle(self, request)

    monkeypatch.setattr(
        capacity_probe.ASGITransport, "handle_async_request", delayed_transport
    )

    def captured_queue_age(database_path: Path, *, wall_started: float) -> float:
        nonlocal age_observed_at
        age_observed_at = time.perf_counter()
        return original_queue_age(database_path, wall_started=wall_started)

    monkeypatch.setattr(capacity_probe, "_oldest_queue_age_ms", captured_queue_age)
    result = await run_probe(
        ProbeConfig(
            thresholds=ProbeThresholds(
                p95_submit_ms=1_000,
                p95_first_event_ms=1_000,
                recovery_ms=1_500,
            )
        )
    )

    assert observed_first_api_arrival is not None
    assert age_observed_at is not None
    assert (
        result["measurements"]["queue_oldest_age_ms"]
        <= (age_observed_at - observed_first_api_arrival) * 1_000 + 10
    )


def test_design_envelopes_label_enterprise_scale_as_unmeasured() -> None:
    envelopes = {item["name"]: item for item in build_design_envelopes()}

    assert envelopes["pilot"]["runs_per_second"] == 1
    assert envelopes["pilot"]["in_flight_range"] == "20-50"
    assert envelopes["business_unit"]["runs_per_second"] == 20
    assert envelopes["business_unit"]["in_flight_range"] == "500-1,500"
    assert envelopes["enterprise_stress"]["runs_per_second"] == 100
    assert envelopes["enterprise_stress"]["in_flight_runs"] == 10_000
    assert envelopes["enterprise_stress"]["in_flight_range"] == "5,000-15,000"
    assert "unmeasured design probe" in envelopes["enterprise_stress"]["label"]
    assert "not a production capacity claim" in envelopes["enterprise_stress"]["label"]


def test_capacity_probe_cli_writes_machine_readable_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "capacity-proof.json"
    original_run_probe = capacity_probe.run_probe

    async def run_with_ci_tolerances() -> dict[str, object]:
        return await original_run_probe(
            ProbeConfig(
                thresholds=ProbeThresholds(
                    p95_submit_ms=1_000,
                    p95_first_event_ms=1_000,
                    recovery_ms=1_500,
                )
            )
        )

    monkeypatch.setattr(capacity_probe, "run_probe", run_with_ci_tolerances)

    assert main(["--output", output_path.as_posix()]) == 0

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["assertions"]["passed"] is True
    assert payload["scenario"]["submissions"] == 12
    assert payload["measurements"]["accepted"] == 8


def test_default_probe_thresholds_match_the_capacity_contract() -> None:
    assert ProbeThresholds() == ProbeThresholds(
        p95_submit_ms=250,
        p95_first_event_ms=350,
        recovery_ms=1_000,
    )
