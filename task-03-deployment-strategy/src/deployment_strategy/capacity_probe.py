"""Repeatable local capacity proof for the Task 3 deployment strategy.

The probe intentionally stays in-process: FastAPI ASGI, SQLite, and a deterministic
fake executor prove the API contracts without creating cloud spend or a second
production implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from httpx import ASGITransport, AsyncClient

from agent_api.app import create_app
from agent_api.security import LimitConfig
from agent_api.storage import (
    SQLiteRunRepository,
    SQLiteTenantRepository,
    SQLiteWorkQueue,
    TenantRecord,
)
from agent_api.workers.local import LocalWorker
from search_agent import (
    EventType,
    FailureReason,
    PublicEvent,
    RunResult,
    RunStateGraph,
    RunUsage,
)
from search_agent.contracts import QueryText

from .container import FakeRunExecutor

_NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
_TENANT_ID = "tenant-capacity"
_SESSION_ID = "session-capacity"
_CORRELATION_ID = "corr-capacity-proof"
_QUERY = "Find the Siemens sustainability report."
_MODEL_QUOTA_QUERY = "Trigger deterministic model quota failure."


@dataclass(frozen=True, slots=True)
class ProbeThresholds:
    p95_submit_ms: float = 250.0
    p95_first_event_ms: float = 250.0
    recovery_ms: float = 1_000.0


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    submissions: int = 12
    max_queued_runs: int = 8
    cancelled_index: int = 3
    model_quota_index: int = 6
    thresholds: ProbeThresholds = ProbeThresholds()


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


class SequentialIds:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._next = 0

    def __call__(self) -> str:
        self._next += 1
        return f"{self._prefix}-{self._next:04d}"


class FakeCapacityExecutor:
    def __init__(self) -> None:
        self._offline = FakeRunExecutor()

    async def run(
        self,
        *,
        tenant_id: str,
        session_id: str,
        run_id: str,
        request: QueryText,
    ) -> RunResult:
        await asyncio.sleep(0.001)
        if request == _MODEL_QUOTA_QUERY:
            return _failed_result(tenant_id, session_id, run_id, request)
        return await self._offline.run(
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
            request=request,
        )


async def run_probe(config: ProbeConfig | None = None) -> dict[str, Any]:
    config = ProbeConfig() if config is None else config
    with TemporaryDirectory(prefix="siemens-capacity-") as workspace:
        database_path = Path(workspace) / "capacity.sqlite3"
        app = create_app(
            database_path=database_path,
            pepper_provider=FixedPepper(),
            clock=lambda: _NOW,
            session_id_factory=lambda: _SESSION_ID,
            run_id_factory=SequentialIds("run-capacity"),
            run_executor=None,
            limit_config=LimitConfig(
                request_burst=200,
                requests_per_second=200.0,
                max_queued_runs=config.max_queued_runs,
                max_concurrent_runs=2,
                max_sse_connections=4,
                daily_work_units=100,
            ),
        )
        before = resource.getrusage(resource.RUSAGE_SELF)
        started = time.perf_counter()
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://capacity.local",
                timeout=5.0,
            ) as client,
        ):
            authorization = await _seed_tenant(app, database_path)
            session = await _create_session(client, authorization)
            queue_wait_started = time.perf_counter()
            submissions = await _submit_burst(client, authorization, config)
            accepted = [item for item in submissions if item["status_code"] == 202]
            rejected = [item for item in submissions if item["status_code"] == 429]
            duplicate = await _duplicate_submit(client, authorization, accepted[0])
            conflict = await _conflicting_duplicate_submit(
                client, authorization, accepted[0]
            )
            status = await _get_status(client, authorization, accepted[0]["run_id"])
            cancel = await _cancel(
                client, authorization, accepted[config.cancelled_index]
            )
            first_event = await _first_event_latency(
                client, authorization, accepted[config.cancelled_index]["run_id"]
            )
            oldest_queue_age_ms = _oldest_queue_age_ms(
                database_path,
                wall_started=queue_wait_started,
            )
            recovery_started = time.perf_counter()
            drained = await _drain(database_path)
            recovery = await _submit_one(
                client,
                authorization,
                idempotency_key="capacity-recovery",
                query=_QUERY,
            )
            recovery_ms = (time.perf_counter() - recovery_started) * 1_000
            if recovery["status_code"] == 202:
                drained += await _drain(database_path)
            first_events = [first_event]
            for item in accepted:
                if item["run_id"] != first_event["run_id"]:
                    first_events.append(
                        await _first_event_latency(
                            client, authorization, item["run_id"]
                        )
                    )
            terminal = await _terminal_summary(client, authorization, accepted)

        elapsed_ms = (time.perf_counter() - started) * 1_000
        after = resource.getrusage(resource.RUSAGE_SELF)
    return _result(
        config=config,
        session_id=session["session_id"],
        submissions=submissions,
        accepted=accepted,
        rejected=rejected,
        duplicate=duplicate,
        conflict=conflict,
        status=status,
        cancel=cancel,
        first_events=first_events,
        oldest_queue_age_ms=oldest_queue_age_ms,
        drained=drained,
        terminal=terminal,
        recovery=recovery,
        recovery_ms=recovery_ms,
        elapsed_ms=elapsed_ms,
        before=before,
        after=after,
    )


def build_design_envelopes() -> tuple[dict[str, Any], ...]:
    return (
        _envelope(
            name="pilot",
            runs_per_second=1,
            mean_run_duration_seconds=35,
            safe_concurrency_per_worker=10,
            mean_searches_per_run=3,
            mean_pages_per_search=4,
            mean_model_calls_per_run=4,
            mean_tokens_per_call=1_500,
            in_flight_range="20-50",
            label="measured locally only for the small fake-provider proof",
        ),
        _envelope(
            name="business_unit",
            runs_per_second=20,
            mean_run_duration_seconds=50,
            safe_concurrency_per_worker=10,
            mean_searches_per_run=4,
            mean_pages_per_search=5,
            mean_model_calls_per_run=5,
            mean_tokens_per_call=2_000,
            in_flight_range="500-1,500",
            label="unmeasured design probe",
        ),
        _envelope(
            name="enterprise_stress",
            runs_per_second=100,
            mean_run_duration_seconds=100,
            safe_concurrency_per_worker=10,
            mean_searches_per_run=5,
            mean_pages_per_search=6,
            mean_model_calls_per_run=6,
            mean_tokens_per_call=2_500,
            in_flight_range="5,000-15,000",
            label="unmeasured design probe; not a production capacity claim",
        ),
    )


def write_json(result: dict[str, Any], path: Path | None) -> None:
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(text, end="")
    else:
        path.write_text(text, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write machine-readable JSON here.")
    args = parser.parse_args(argv)
    result = asyncio.run(run_probe())
    write_json(result, args.output)
    return 0 if result["assertions"]["passed"] else 1


async def _seed_tenant(app: Any, database_path: Path) -> str:
    await SQLiteTenantRepository(database_path).put(
        TenantRecord(tenant_id=_TENANT_ID, created_at=_NOW)
    )
    generated = await app.state.auth_manager.create(
        tenant_id=_TENANT_ID,
        scopes=("sessions:write", "runs:read", "runs:write"),
        now=_NOW,
    )
    return f"Bearer {generated.plaintext}"


async def _create_session(client: AsyncClient, authorization: str) -> dict[str, Any]:
    response = await client.post(
        "/v1/sessions",
        json={"label": "Capacity proof"},
        headers=_headers(authorization),
    )
    response.raise_for_status()
    return dict(response.json())


async def _submit_burst(
    client: AsyncClient, authorization: str, config: ProbeConfig
) -> list[dict[str, Any]]:
    async def submit(index: int) -> dict[str, Any]:
        query = _MODEL_QUOTA_QUERY if index == config.model_quota_index else _QUERY
        return await _submit_one(
            client,
            authorization,
            idempotency_key=f"capacity-{index:04d}",
            query=query,
        )

    accepted_wave = await asyncio.gather(
        *(submit(index) for index in range(config.max_queued_runs))
    )
    rejected_wave = await asyncio.gather(
        *(submit(index) for index in range(config.max_queued_runs, config.submissions))
    )
    return [*accepted_wave, *rejected_wave]


async def _submit_one(
    client: AsyncClient,
    authorization: str,
    *,
    idempotency_key: str,
    query: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    response = await client.post(
        f"/v1/sessions/{_SESSION_ID}/runs",
        json={"query": query},
        headers={**_headers(authorization), "Idempotency-Key": idempotency_key},
    )
    elapsed_ms = (time.perf_counter() - started) * 1_000
    body = response.json()
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "idempotency_key": idempotency_key,
        "query": query,
        "run_id": body.get("run_id"),
        "status_code": response.status_code,
    }


async def _duplicate_submit(
    client: AsyncClient, authorization: str, accepted: dict[str, Any]
) -> dict[str, Any]:
    return await _submit_one(
        client,
        authorization,
        idempotency_key=accepted["idempotency_key"],
        query=accepted["query"],
    )


async def _conflicting_duplicate_submit(
    client: AsyncClient, authorization: str, accepted: dict[str, Any]
) -> dict[str, Any]:
    return await _submit_one(
        client,
        authorization,
        idempotency_key=accepted["idempotency_key"],
        query="Find a different public capacity proof.",
    )


async def _get_status(
    client: AsyncClient, authorization: str, run_id: str
) -> dict[str, Any]:
    response = await client.get(f"/v1/runs/{run_id}", headers=_headers(authorization))
    response.raise_for_status()
    body = response.json()
    failure = body.get("failure")
    return {
        "failure_code": None if failure is None else failure["code"],
        "run_id": body["run_id"],
        "state": body["state"],
    }


async def _cancel(
    client: AsyncClient, authorization: str, accepted: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post(
        f"/v1/runs/{accepted['run_id']}/cancel",
        headers=_headers(authorization),
    )
    response.raise_for_status()
    body = response.json()
    return {
        "changed": body["changed"],
        "run_id": body["run_id"],
        "state": body["state"],
    }


async def _first_event_latency(
    client: AsyncClient, authorization: str, run_id: str
) -> dict[str, Any]:
    started = time.perf_counter()
    response = await client.get(
        f"/v1/runs/{run_id}/events",
        headers={**_headers(authorization), "Last-Event-ID": "1"},
    )
    response.raise_for_status()
    elapsed_ms = (time.perf_counter() - started) * 1_000
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "event_type": _sse_field(response.text, "event"),
        "run_id": run_id,
    }


def _oldest_queue_age_ms(database_path: Path, *, wall_started: float) -> float:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM work_items").fetchone()
    if row is None or row[0] == 0:
        return 0.0
    return round((time.perf_counter() - wall_started) * 1_000, 3)


async def _drain(database_path: Path) -> int:
    worker = LocalWorker(
        repository=SQLiteRunRepository(database_path),
        queue=SQLiteWorkQueue(database_path),
        executor=FakeCapacityExecutor(),
        worker_id="worker-capacity",
        clock=lambda: _NOW,
        heartbeat_seconds=1.0,
        lease_seconds=30,
        visibility_seconds=30,
    )
    processed = 0
    while await worker.process_one():
        processed += 1
    return processed


async def _terminal_summary(
    client: AsyncClient, authorization: str, accepted: list[dict[str, Any]]
) -> dict[str, int]:
    counts = {"budget_exhausted": 0, "cancelled": 0, "completed": 0}
    for item in accepted:
        status = await _get_status(client, authorization, item["run_id"])
        if status["state"] in counts:
            counts[status["state"]] += 1
        if status["failure_code"] == "budget_exhausted":
            counts["budget_exhausted"] += 1
    return counts


def _result(
    *,
    config: ProbeConfig,
    session_id: str,
    submissions: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    duplicate: dict[str, Any],
    conflict: dict[str, Any],
    status: dict[str, Any],
    cancel: dict[str, Any],
    first_events: list[dict[str, Any]],
    oldest_queue_age_ms: float,
    drained: int,
    terminal: dict[str, int],
    recovery: dict[str, Any],
    recovery_ms: float,
    elapsed_ms: float,
    before: resource.struct_rusage,
    after: resource.struct_rusage,
) -> dict[str, Any]:
    measurements = {
        "accepted": len(accepted),
        "duplicate_status_code": duplicate["status_code"],
        "duplicate_run_id_matches": duplicate["run_id"] == accepted[0]["run_id"],
        "p95_first_event_ms": _p95(event["elapsed_ms"] for event in first_events),
        "p95_submit_ms": _p95(item["elapsed_ms"] for item in submissions),
        "queue_oldest_age_ms": oldest_queue_age_ms,
        "recovery_accepted": recovery["status_code"] == 202,
        "recovery_ms": round(recovery_ms, 3),
        "rejected": len(rejected),
        "resource_usage": {
            "elapsed_ms": round(elapsed_ms, 3),
            "max_rss_delta": after.ru_maxrss - before.ru_maxrss,
            "max_rss_unit": "platform_ru_maxrss",
            "system_cpu_seconds_delta": round(after.ru_stime - before.ru_stime, 6),
            "user_cpu_seconds_delta": round(after.ru_utime - before.ru_utime, 6),
        },
        "terminal": terminal,
    }
    assertions = {
        "accepted_equals_queue_limit": len(accepted) == config.max_queued_runs,
        "cancelled_run_terminal": cancel["changed"] and cancel["state"] == "cancelled",
        "conflicting_duplicate_rejected": conflict["status_code"] == 409,
        "duplicate_is_idempotent": measurements["duplicate_run_id_matches"],
        "first_event_is_cancelled": first_events[0]["event_type"] == "run.cancelled",
        "model_quota_failure_observed": terminal["budget_exhausted"] == 1,
        "recovery_within_threshold": measurements["recovery_ms"]
        <= config.thresholds.recovery_ms,
        "rejected_when_queue_full": len(rejected)
        == config.submissions - config.max_queued_runs,
        "status_after_submit_is_queued": status["state"] == "queued",
        "submit_p95_within_threshold": measurements["p95_submit_ms"]
        <= config.thresholds.p95_submit_ms,
        "sse_p95_within_threshold": measurements["p95_first_event_ms"]
        <= config.thresholds.p95_first_event_ms,
    }
    assertions["passed"] = all(assertions.values())
    return {
        "assertions": assertions,
        "design_envelopes": list(build_design_envelopes()),
        "measurements": measurements,
        "scenario": {
            "cancelled_index": config.cancelled_index,
            "drained_work_items": drained,
            "max_queued_runs": config.max_queued_runs,
            "model_quota_index": config.model_quota_index,
            "session_id": session_id,
            "submissions": config.submissions,
        },
        "schema_version": 1,
    }


def _failed_result(
    tenant_id: str, session_id: str, run_id: str, request: str
) -> RunResult:
    snapshot = RunStateGraph.create(tenant_id, session_id, run_id, request)
    events = [_created(snapshot)]
    snapshot, event = RunStateGraph.fail(
        snapshot,
        FailureReason.BUDGET_EXHAUSTED,
        message="Fake model quota was exhausted within policy bounds.",
    )
    events.append(event)
    return RunResult(snapshot=snapshot, events=tuple(events), usage=_usage())


def _created(snapshot: Any) -> PublicEvent:
    return PublicEvent(
        tenant_id=snapshot.tenant_id,
        session_id=snapshot.session_id,
        run_id=snapshot.run_id,
        event_type=EventType.RUN_CREATED,
        message="Created bounded research run",
    )


def _usage() -> RunUsage:
    return RunUsage(
        elapsed_seconds=0.001,
        iterations=1,
        search_queries=1,
        pages=1,
        failed_pages=0,
        raw_bytes_reserved=0,
        decoded_bytes=0,
        model_calls=1,
        model_attempts=1,
        tokens=128,
    )


def _envelope(
    *,
    name: str,
    runs_per_second: int,
    mean_run_duration_seconds: int,
    safe_concurrency_per_worker: int,
    mean_searches_per_run: int,
    mean_pages_per_search: int,
    mean_model_calls_per_run: int,
    mean_tokens_per_call: int,
    in_flight_range: str,
    label: str,
) -> dict[str, Any]:
    in_flight = runs_per_second * mean_run_duration_seconds
    return {
        "in_flight_runs": in_flight,
        "in_flight_range": in_flight_range,
        "label": label,
        "name": name,
        "peak_fetches_per_second": runs_per_second
        * mean_searches_per_run
        * mean_pages_per_search,
        "required_model_tokens_per_second": runs_per_second
        * mean_model_calls_per_run
        * mean_tokens_per_call,
        "runs_per_second": runs_per_second,
        "worker_slots": (in_flight + safe_concurrency_per_worker - 1)
        // safe_concurrency_per_worker,
    }


def _headers(authorization: str) -> dict[str, str]:
    return {"Authorization": authorization, "X-Correlation-ID": _CORRELATION_ID}


def _p95(values: Iterable[float]) -> float:
    measured = sorted(float(value) for value in values)
    if not measured:
        return 0.0
    rank = max(0, int(0.95 * len(measured) + 0.999999) - 1)
    return round(measured[rank], 3)


def _sse_field(text: str, field: str) -> str | None:
    prefix = f"{field}: "
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


if __name__ == "__main__":
    raise SystemExit(main())
