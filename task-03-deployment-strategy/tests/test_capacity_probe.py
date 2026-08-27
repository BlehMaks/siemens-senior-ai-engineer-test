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
async def test_queue_age_starts_after_client_side_submit_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_post = capacity_probe.AsyncClient.post
    original_queue_age = capacity_probe._oldest_queue_age_ms
    observed_first_api_attempt: float | None = None
    age_observed_at: float | None = None

    async def delayed_submit(self, url, *args, **kwargs):
        nonlocal observed_first_api_attempt
        key = kwargs.get("headers", {}).get("Idempotency-Key", "")
        if str(url).endswith("/runs") and key.startswith("capacity-"):
            await asyncio.sleep(0.1)
            now = time.perf_counter()
            if observed_first_api_attempt is None or now < observed_first_api_attempt:
                observed_first_api_attempt = now
        return await original_post(self, url, *args, **kwargs)

    monkeypatch.setattr(capacity_probe.AsyncClient, "post", delayed_submit)

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

    assert observed_first_api_attempt is not None
    assert age_observed_at is not None
    assert (
        result["measurements"]["queue_oldest_age_ms"]
        <= (age_observed_at - observed_first_api_attempt) * 1_000 + 10
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


def test_capacity_probe_cli_writes_machine_readable_json(tmp_path: Path) -> None:
    output_path = tmp_path / "capacity-proof.json"

    assert main(["--output", output_path.as_posix()]) == 0

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["assertions"]["passed"] is True
    assert payload["scenario"]["submissions"] == 12
    assert payload["measurements"]["accepted"] == 8
