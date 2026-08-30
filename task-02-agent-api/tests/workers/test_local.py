from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import AnyHttpUrl

from agent_api import RunRepository
from agent_api.observability import OperationalTelemetry
from agent_api.ports import (
    ClaimDisposition,
    ClaimRequest,
    ClaimResult,
    ExecutionLease,
    LeaseDisposition,
    LeaseRenewal,
    LeaseResult,
    RunFailureCode,
    RunRecord,
    RunState,
    RunSubmission,
    StateUpdate,
    StateUpdateResult,
    WorkItem,
    WriteDisposition,
)
from agent_api.storage import (
    AuditEntry,
    SessionRecord,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    SQLiteWorkQueue,
    TenantRecord,
    migrate,
)
from agent_api.workers import LocalWorker
from search_agent import (
    Citation,
    ExtractedEvidence,
    FailureReason,
    OptionalAssistance,
    PublicEvent,
    QueryPlan,
    RunResult,
    RunSnapshot,
    RunStateGraph,
    RunUsage,
    ScopedAnswer,
    SearchHit,
    SearchQuery,
    ToolBudget,
)
from search_agent.contracts import EventType, OpaqueId
from search_agent.memory import RunReflection

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class AuditRecorder:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def append(self, entry: AuditEntry) -> bool:
        self.entries.append(entry)
        return True


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeQueue:
    def __init__(self, *items: WorkItem, cancel_error: Exception | None = None) -> None:
        self._items = {item.work_id: item for item in items}
        self.cancel_calls: list[tuple[str, str]] = []
        self._lock = asyncio.Lock()
        self._cancel_error = cancel_error

    async def receive(
        self, *, now: datetime, visibility_seconds: int
    ) -> WorkItem | None:
        async with self._lock:
            available = [
                item for item in self._items.values() if item.not_before <= now
            ]
            if not available:
                return None
            item = min(
                available,
                key=lambda current: (current.not_before, current.work_id),
            )
            self._items[item.work_id] = item.model_copy(
                update={"not_before": now + timedelta(seconds=visibility_seconds)}
            )
            return item

    async def cancel(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        generation_id: OpaqueId | None = None,
    ) -> int:
        async with self._lock:
            if self._cancel_error is not None:
                error, self._cancel_error = self._cancel_error, None
                raise error
            keys = [
                work_id
                for work_id, item in self._items.items()
                if item.tenant_id == tenant_id
                and item.run_id == run_id
                and (generation_id is None or item.generation_id == generation_id)
            ]
            for key in keys:
                del self._items[key]
            self.cancel_calls.append((tenant_id, run_id))
            return len(keys)

    async def discard(self, item: WorkItem) -> bool:
        async with self._lock:
            if self._items.get(item.work_id) != item:
                return False
            del self._items[item.work_id]
            return True


class FakeRunRepository:
    def __init__(self, *runs: RunRecord) -> None:
        self._runs = {(run.tenant_id, run.run_id): run for run in runs}
        self.reflections: dict[tuple[str, str, str], RunReflection] = {}
        self._lock = asyncio.Lock()
        self.renewed = asyncio.Event()

    async def create(self, submission: RunSubmission) -> object:
        raise NotImplementedError

    async def get(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> RunRecord | None:
        async with self._lock:
            return self._runs.get((tenant_id, run_id))

    async def remove(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> None:
        async with self._lock:
            self._runs.pop((tenant_id, run_id), None)

    async def list_session(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, limit: int = 100
    ) -> tuple[RunRecord, ...]:
        del tenant_id, session_id, limit
        raise NotImplementedError

    async def claim(self, request: ClaimRequest) -> ClaimResult:
        async with self._lock:
            run = self._runs.get((request.tenant_id, request.run_id))
            if run is None:
                return ClaimResult(disposition=ClaimDisposition.NOT_FOUND, run=None)
            if run.state in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
                RunState.EXPIRED,
            }:
                return ClaimResult(disposition=ClaimDisposition.TERMINAL, run=run)
            if run.cancellation_requested_at is not None:
                if run.lease is not None and run.lease.expires_at > request.now:
                    return ClaimResult(
                        disposition=ClaimDisposition.CANCELLATION_REQUESTED,
                        run=run,
                    )
                cancelled = _copy_run(
                    run,
                    state=RunState.CANCELLED,
                    updated_at=request.now,
                    terminal_at=request.now,
                    lease=None,
                )
                self._runs[(run.tenant_id, run.run_id)] = cancelled
                return ClaimResult(
                    disposition=ClaimDisposition.CANCELLATION_REQUESTED,
                    run=cancelled,
                )
            if run.lease is not None and run.lease.expires_at > request.now:
                return ClaimResult(disposition=ClaimDisposition.BUSY, run=run)
            claimed = _copy_run(
                run,
                state=RunState.RUNNING,
                updated_at=request.now,
                delivery_attempts=run.delivery_attempts + 1,
                lease=ExecutionLease(
                    lease_id=request.lease_id,
                    worker_id=request.worker_id,
                    acquired_at=request.now,
                    expires_at=request.now + timedelta(seconds=request.lease_seconds),
                ),
            )
            self._runs[(run.tenant_id, run.run_id)] = claimed
            return ClaimResult(disposition=ClaimDisposition.CLAIMED, run=claimed)

    async def renew_lease(self, renewal: LeaseRenewal) -> LeaseResult:
        async with self._lock:
            run = self._runs.get((renewal.tenant_id, renewal.run_id))
            if run is None:
                return LeaseResult(disposition=LeaseDisposition.NOT_FOUND, run=None)
            if run.state in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
                RunState.EXPIRED,
            }:
                return LeaseResult(disposition=LeaseDisposition.TERMINAL, run=run)
            if run.cancellation_requested_at is not None:
                return LeaseResult(
                    disposition=LeaseDisposition.CANCELLATION_REQUESTED,
                    run=run,
                )
            lease = run.lease
            if (
                lease is None
                or lease.lease_id != renewal.lease_id
                or lease.worker_id != renewal.worker_id
                or lease.expires_at <= renewal.now
            ):
                return LeaseResult(disposition=LeaseDisposition.LOST, run=run)
            renewed = _copy_run(
                run,
                updated_at=renewal.now,
                lease=lease.model_copy(
                    update={
                        "expires_at": max(
                            lease.expires_at,
                            renewal.now + timedelta(seconds=renewal.lease_seconds),
                        )
                    }
                ),
            )
            self._runs[(run.tenant_id, run.run_id)] = renewed
            self.renewed.set()
            return LeaseResult(disposition=LeaseDisposition.RENEWED, run=renewed)

    async def compare_and_set(self, update: StateUpdate) -> StateUpdateResult:
        async with self._lock:
            run = self._runs.get((update.tenant_id, update.run_id))
            if run is None:
                return StateUpdateResult(
                    disposition=WriteDisposition.NOT_FOUND,
                    run=None,
                )
            if (
                run.version != update.expected_version
                or run.state is not update.expected_state
                or update.at < run.updated_at
            ):
                return StateUpdateResult(disposition=WriteDisposition.CONFLICT, run=run)
            if update.lease_id is not None:
                lease = run.lease
                if (
                    lease is None
                    or lease.lease_id != update.lease_id
                    or lease.worker_id != update.worker_id
                    or lease.expires_at <= update.at
                ):
                    return StateUpdateResult(
                        disposition=WriteDisposition.LEASE_LOST,
                        run=run,
                    )
            if (
                run.cancellation_requested_at is not None
                and update.next_state is not RunState.CANCELLED
            ):
                return StateUpdateResult(
                    disposition=WriteDisposition.CANCELLATION_REQUESTED,
                    run=run,
                )
            terminal = update.next_state in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
                RunState.EXPIRED,
            }
            changed = _copy_run(
                run,
                state=update.next_state,
                updated_at=update.at,
                terminal_at=update.at if terminal else None,
                lease=None if terminal else run.lease,
                answer=update.answer,
                failure_code=update.failure_code,
                memory_used=update.memory_used,
                cancellation_requested_at=(
                    run.cancellation_requested_at
                    if update.next_state is not RunState.CANCELLED
                    else (run.cancellation_requested_at or update.at)
                ),
            )
            self._runs[(run.tenant_id, run.run_id)] = changed
            if update.reflection is not None:
                self.reflections[
                    (
                        update.reflection.tenant_id,
                        update.reflection.session_id,
                        update.reflection.run_id,
                    )
                ] = update.reflection
            return StateUpdateResult(disposition=WriteDisposition.APPLIED, run=changed)

    async def request_cancellation(
        self, *, tenant_id: OpaqueId, run_id: OpaqueId, at: datetime
    ) -> None:
        async with self._lock:
            run = self._runs[(tenant_id, run_id)]
            self._runs[(tenant_id, run_id)] = _copy_run(
                run,
                updated_at=at,
                cancellation_requested_at=at,
            )


class CancelBeforeCompletionRepository(FakeRunRepository):
    def __init__(self, *runs: RunRecord) -> None:
        super().__init__(*runs)
        self._inject_cancellation = True

    async def compare_and_set(self, update: StateUpdate) -> StateUpdateResult:
        if self._inject_cancellation and update.next_state is RunState.COMPLETED:
            self._inject_cancellation = False
            await self.request_cancellation(
                tenant_id=update.tenant_id,
                run_id=update.run_id,
                at=update.at,
            )
        return await super().compare_and_set(update)


@dataclass(slots=True)
class ScriptedExecutor:
    result: RunResult | None = None
    error: BaseException | None = None
    block: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)

    async def run(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        request: str,
    ) -> RunResult:
        self.calls.append((tenant_id, session_id, run_id, request))
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@pytest.mark.asyncio
async def test_two_workers_execute_one_delivery() -> None:
    run = queued_run()
    queue = FakeQueue(work_item())
    repository = FakeRunRepository(run)
    executor = ScriptedExecutor(result=completed_result())
    worker_one = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=queue,
        executor=executor,
        worker_id="worker-one",
        clock=MutableClock(NOW),
        lease_id_factory=lambda: "lease-one",
    )
    worker_two = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=queue,
        executor=executor,
        worker_id="worker-two",
        clock=MutableClock(NOW),
        lease_id_factory=lambda: "lease-two",
    )

    first, second = await asyncio.gather(
        worker_one.process_one(),
        worker_two.process_one(),
    )
    stored = await repository.get(tenant_id="tenant-one", run_id="run-one")

    assert {first, second} == {True, False}
    assert executor.calls == [
        ("tenant-one", "session-one", "run-one", "find the documented answer")
    ]
    assert stored is not None
    assert stored.state is RunState.COMPLETED
    assert stored.delivery_attempts == 1


@pytest.mark.asyncio
async def test_worker_persists_truthful_reviewed_memory_indicator() -> None:
    queue = FakeQueue(work_item())
    repository = FakeRunRepository(queued_run())
    result = completed_result()
    result = RunResult(
        snapshot=result.snapshot,
        events=result.events,
        usage=result.usage.model_copy(update={"memory_records": 2}),
    )
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=queue,
        executor=ScriptedExecutor(result=result),
        worker_id="worker-one",
        clock=MutableClock(NOW),
        lease_id_factory=lambda: "lease-one",
    )

    await worker.process_one()
    stored = await repository.get(tenant_id="tenant-one", run_id="run-one")

    assert stored is not None
    assert stored.memory_used is True
    reflection = repository.reflections[("tenant-one", "session-one", "run-one")]
    assert reflection.usage.memory_records == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build_executor", "expected_state", "expected_failure"),
    [
        (lambda: ScriptedExecutor(result=completed_result()), RunState.COMPLETED, None),
        (
            lambda: ScriptedExecutor(
                result=failed_result(reason=FailureReason.SEARCH_FAILED),
            ),
            RunState.FAILED,
            RunFailureCode.SEARCH_FAILED,
        ),
        (
            lambda: ScriptedExecutor(result=cancelled_result()),
            RunState.CANCELLED,
            None,
        ),
        (
            lambda: ScriptedExecutor(error=RuntimeError("do not leak")),
            RunState.FAILED,
            RunFailureCode.EXECUTION_FAILED,
        ),
        (
            lambda: ScriptedExecutor(error=asyncio.CancelledError()),
            RunState.FAILED,
            RunFailureCode.EXECUTION_FAILED,
        ),
        (
            lambda: ScriptedExecutor(result=cast(RunResult, object())),
            RunState.FAILED,
            RunFailureCode.EXECUTION_FAILED,
        ),
    ],
)
async def test_terminal_mapping_persists_public_projection(
    build_executor: Callable[[], ScriptedExecutor],
    expected_state: RunState,
    expected_failure: RunFailureCode | None,
) -> None:
    queue = FakeQueue(work_item())
    repository = FakeRunRepository(queued_run())
    executor = build_executor()
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=queue,
        executor=executor,
        worker_id="worker-one",
        clock=MutableClock(NOW),
        lease_id_factory=lambda: "lease-one",
    )

    await worker.process_one()
    stored = await repository.get(tenant_id="tenant-one", run_id="run-one")

    assert stored is not None
    assert stored.state is expected_state
    assert stored.failure_code is expected_failure
    assert stored.terminal_at == NOW
    if expected_state is RunState.COMPLETED:
        assert stored.answer == completed_result().snapshot.answer
    else:
        assert stored.answer is None
    if (
        expected_state is RunState.FAILED
        and expected_failure is RunFailureCode.EXECUTION_FAILED
    ):
        assert repository.reflections == {}
    else:
        reflection = repository.reflections[("tenant-one", "session-one", "run-one")]
        assert isinstance(reflection, RunReflection)


@pytest.mark.asyncio
async def test_worker_emits_safe_failed_run_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = FakeQueue(work_item())
    repository = FakeRunRepository(queued_run())
    audit = AuditRecorder()
    logger = logging.getLogger("agent_api.operations.worker-test")
    logger.setLevel(logging.INFO)
    telemetry = OperationalTelemetry(
        pseudonym_key=b"t" * 32,
        audit=audit,
        logger=logger,
    )
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=queue,
        executor=ScriptedExecutor(
            result=failed_result(reason=FailureReason.SEARCH_FAILED)
        ),
        worker_id="worker-one",
        clock=MutableClock(NOW),
        lease_id_factory=lambda: "lease-one",
        telemetry=telemetry,
    )

    with caplog.at_level(logging.INFO, logger=logger.name):
        await worker.process_one()

    payloads = [json.loads(record.message) for record in caplog.records]
    assert [payload["event"] for payload in payloads] == [
        "worker.outcome",
        "run.terminal",
        "worker.outcome",
    ]
    terminal = payloads[1]
    assert terminal["state"] == "failed"
    assert terminal["failure"] == "search_failed"
    assert (
        terminal["pages"]
        == failed_result(reason=FailureReason.SEARCH_FAILED).usage.pages
    )
    serialized = "\n".join(record.message for record in caplog.records)
    for forbidden in ("tenant-one", "session-one", "run-one", queued_run().query):
        assert forbidden not in serialized
    assert [(entry.action, entry.tenant_id) for entry in audit.entries] == [
        ("run.failed", "tenant-one")
    ]
    assert all("tenant" not in dict(sample.labels) for sample in telemetry.snapshot())


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_result", ["request", "answer"])
async def test_invalid_executor_result_becomes_safe_terminal_failure(
    invalid_result: str,
) -> None:
    result = completed_result()
    snapshot = result.snapshot
    if invalid_result == "request":
        snapshot = RunSnapshot.model_validate(
            snapshot.model_copy(update={"request": "answer a different request"})
        )
    else:
        assert snapshot.answer is not None
        answer = snapshot.answer.model_copy(
            update={
                "assistance": OptionalAssistance(
                    offer="Review related documented questions.",
                    follow_up_queries=tuple(
                        f"Find documented follow up {index}." for index in range(9)
                    ),
                )
            }
        )
        snapshot = RunSnapshot.model_validate(
            snapshot.model_copy(update={"answer": answer})
        )
    queue = FakeQueue(work_item())
    repository = FakeRunRepository(queued_run())
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=queue,
        executor=ScriptedExecutor(
            result=RunResult(
                snapshot=snapshot,
                events=result.events,
                usage=result.usage,
            )
        ),
        worker_id="worker-one",
        clock=MutableClock(NOW),
        lease_id_factory=lambda: "lease-one",
    )

    await worker.process_one()

    stored = await repository.get(tenant_id="tenant-one", run_id="run-one")
    assert stored is not None
    assert stored.state is RunState.FAILED
    assert stored.failure_code is RunFailureCode.EXECUTION_FAILED
    assert stored.answer is None
    assert repository.reflections == {}


@pytest.mark.asyncio
async def test_heartbeat_renews_lease_until_execution_finishes() -> None:
    block = asyncio.Event()
    executor = ScriptedExecutor(result=completed_result(), block=block)
    repository = FakeRunRepository(queued_run())
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=FakeQueue(work_item()),
        executor=executor,
        worker_id="worker-one",
        clock=MutableClock(NOW),
        heartbeat_seconds=0.01,
        lease_seconds=1,
        lease_id_factory=lambda: "lease-one",
    )

    processing = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(repository.renewed.wait(), timeout=1)
    block.set()
    await processing

    stored = await repository.get(tenant_id="tenant-one", run_id="run-one")
    assert stored is not None and stored.state is RunState.COMPLETED


@pytest.mark.asyncio
async def test_persisted_cancellation_wins_running_execution_race() -> None:
    block = asyncio.Event()
    executor = ScriptedExecutor(result=completed_result(), block=block)
    repository = FakeRunRepository(queued_run())
    queue = FakeQueue(work_item())
    audit = AuditRecorder()
    telemetry = OperationalTelemetry(pseudonym_key=b"t" * 32, audit=audit)
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=queue,
        executor=executor,
        worker_id="worker-one",
        clock=MutableClock(NOW),
        heartbeat_seconds=0.01,
        lease_seconds=1,
        lease_id_factory=lambda: "lease-one",
        telemetry=telemetry,
    )

    processing = asyncio.create_task(worker.process_one())
    await executor.started.wait()
    await repository.request_cancellation(
        tenant_id="tenant-one",
        run_id="run-one",
        at=NOW,
    )
    await processing

    stored = await repository.get(tenant_id="tenant-one", run_id="run-one")
    assert stored is not None
    assert stored.state is RunState.CANCELLED
    assert stored.cancellation_requested_at == NOW
    assert queue.cancel_calls == [("tenant-one", "run-one")]

    duplicate = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=FakeQueue(work_item().model_copy(update={"work_id": "work-two"})),
        executor=executor,
        worker_id="worker-two",
        clock=MutableClock(NOW),
        heartbeat_seconds=0.01,
        lease_seconds=1,
        lease_id_factory=lambda: "lease-two",
        telemetry=telemetry,
    )
    await duplicate.process_one()

    terminal = [
        sample
        for sample in telemetry.snapshot()
        if sample.name == "api_runs_terminal_total"
        and dict(sample.labels)["state"] == "cancelled"
    ]
    assert len(terminal) == 1 and terminal[0].value == 1
    assert [entry.action for entry in audit.entries].count("run.cancelled") == 1


@pytest.mark.asyncio
async def test_applied_cancellation_is_observed_before_queue_cleanup_failure() -> None:
    block = asyncio.Event()
    executor = ScriptedExecutor(result=completed_result(), block=block)
    repository = FakeRunRepository(queued_run())
    audit = AuditRecorder()
    telemetry = OperationalTelemetry(pseudonym_key=b"t" * 32, audit=audit)
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=FakeQueue(
            work_item(), cancel_error=RuntimeError("queue cleanup unavailable")
        ),
        executor=executor,
        worker_id="worker-one",
        clock=MutableClock(NOW),
        heartbeat_seconds=0.01,
        lease_seconds=1,
        lease_id_factory=lambda: "lease-one",
        telemetry=telemetry,
    )

    processing = asyncio.create_task(worker.process_one())
    await executor.started.wait()
    await repository.request_cancellation(
        tenant_id="tenant-one", run_id="run-one", at=NOW
    )
    with pytest.raises(RuntimeError, match="queue cleanup unavailable"):
        await processing

    stored = await repository.get(tenant_id="tenant-one", run_id="run-one")
    assert stored is not None and stored.state is RunState.CANCELLED
    assert [entry.action for entry in audit.entries].count("run.cancelled") == 1
    terminal = [
        sample
        for sample in telemetry.snapshot()
        if sample.name == "api_runs_terminal_total"
        and dict(sample.labels)["state"] == "cancelled"
    ]
    assert len(terminal) == 1 and terminal[0].value == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build_executor", "expected_state", "expected_action"),
    [
        (
            lambda: ScriptedExecutor(result=completed_result()),
            RunState.COMPLETED,
            "run.completed",
        ),
        (
            lambda: ScriptedExecutor(error=RuntimeError("private executor failure")),
            RunState.FAILED,
            "run.failed",
        ),
    ],
)
async def test_applied_result_is_observed_before_queue_cleanup_failure(
    build_executor: Callable[[], ScriptedExecutor],
    expected_state: RunState,
    expected_action: str,
) -> None:
    repository = FakeRunRepository(queued_run())
    queue = FakeQueue(work_item(), cancel_error=RuntimeError("cleanup unavailable"))
    audit = AuditRecorder()
    telemetry = OperationalTelemetry(pseudonym_key=b"t" * 32, audit=audit)
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=queue,
        executor=build_executor(),
        worker_id="worker-one",
        clock=MutableClock(NOW),
        lease_id_factory=lambda: "lease-one",
        telemetry=telemetry,
    )

    with pytest.raises(RuntimeError, match="cleanup unavailable"):
        await worker.process_one()
    await worker.process(work_item())

    stored = await repository.get(tenant_id="tenant-one", run_id="run-one")
    assert stored is not None and stored.state is expected_state
    terminal = [
        sample
        for sample in telemetry.snapshot()
        if sample.name == "api_runs_terminal_total"
        and dict(sample.labels)["state"] == expected_state.value
    ]
    assert len(terminal) == 1 and terminal[0].value == 1
    assert [entry.action for entry in audit.entries].count(expected_action) == 1


@pytest.mark.asyncio
async def test_completion_conflict_terminalizes_same_lease_cancellation() -> None:
    repository = CancelBeforeCompletionRepository(queued_run())
    queue = FakeQueue(work_item())
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=queue,
        executor=ScriptedExecutor(result=completed_result()),
        worker_id="worker-one",
        clock=MutableClock(NOW),
        lease_id_factory=lambda: "lease-one",
    )

    await worker.process_one()

    stored = await repository.get(tenant_id="tenant-one", run_id="run-one")
    assert stored is not None
    assert stored.state is RunState.CANCELLED
    assert stored.cancellation_requested_at == NOW
    assert queue.cancel_calls == [("tenant-one", "run-one")]


@pytest.mark.asyncio
async def test_deleted_run_discards_completed_queue_delivery() -> None:
    block = asyncio.Event()
    executor = ScriptedExecutor(result=completed_result(), block=block)
    repository = FakeRunRepository(queued_run())
    queue = FakeQueue(work_item())
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=queue,
        executor=executor,
        worker_id="worker-one",
        clock=MutableClock(NOW),
        lease_id_factory=lambda: "lease-one",
    )

    processing = asyncio.create_task(worker.process_one())
    await executor.started.wait()
    await repository.remove(tenant_id="tenant-one", run_id="run-one")
    block.set()
    await processing

    assert queue.cancel_calls == [("tenant-one", "run-one")]


@pytest.mark.asyncio
async def test_cancelled_error_during_shutdown_leaves_work_recoverable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worker.sqlite3"
    await migrate(database_path)
    await SQLiteTenantRepository(database_path).put(
        TenantRecord(tenant_id="tenant-one", created_at=NOW)
    )
    await SQLiteSessionRepository(database_path).put(
        SessionRecord(
            tenant_id="tenant-one",
            session_id="session-one",
            label="Alpha",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    runs = SQLiteRunRepository(database_path)
    await runs.create(
        RunSubmission(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-one",
            idempotency_key="request-key-one",
            query="find the documented answer",
            created_at=NOW,
        )
    )
    queue = SQLiteWorkQueue(database_path)
    await queue.enqueue(work_item(generation_id=None))

    block = asyncio.Event()
    executor = ScriptedExecutor(result=completed_result(), block=block)
    clock = MutableClock(NOW)
    worker = LocalWorker(
        repository=runs,
        queue=queue,
        executor=executor,
        worker_id="worker-one",
        clock=clock,
        lease_id_factory=lambda: "lease-one",
    )

    task = asyncio.create_task(worker.process_one())
    await executor.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await runs.get(tenant_id="tenant-one", run_id="run-one")
    assert stored is not None
    assert stored.state is RunState.RUNNING
    assert stored.lease is not None
    assert stored.terminal_at is None

    clock.advance(seconds=30)
    received = await queue.receive(now=clock(), visibility_seconds=30)
    recovered = await runs.claim(
        ClaimRequest(
            tenant_id="tenant-one",
            run_id="run-one",
            worker_id="worker-two",
            lease_id="lease-two",
            now=clock(),
            lease_seconds=30,
        )
    )

    assert received is not None
    assert received.work_id == "work-one"
    assert received.tenant_id == "tenant-one"
    assert received.run_id == "run-one"
    assert received.enqueued_at == NOW
    assert recovered.disposition is ClaimDisposition.CLAIMED
    assert recovered.run is not None
    assert recovered.run.delivery_attempts == 2


def queued_run() -> RunRecord:
    return RunRecord(
        tenant_id="tenant-one",
        session_id="session-one",
        run_id="run-one",
        generation_id="generation-test",
        idempotency_key="request-key-one",
        query="find the documented answer",
        state=RunState.QUEUED,
        version=0,
        delivery_attempts=0,
        created_at=NOW,
        updated_at=NOW,
    )


def work_item(
    *, not_before: datetime = NOW, generation_id: str | None = "generation-test"
) -> WorkItem:
    return WorkItem(
        work_id="work-one",
        tenant_id="tenant-one",
        run_id="run-one",
        generation_id=generation_id,
        enqueued_at=NOW,
        not_before=not_before,
    )


def completed_result() -> RunResult:
    snapshot = RunStateGraph.create(
        "tenant-one",
        "session-one",
        "run-one",
        "find the documented answer",
    )
    events = [_created(snapshot)]
    plan = QueryPlan(
        tool_budget=ToolBudget(max_search_queries=1, max_fetches=1),
        searches=(SearchQuery(text="find Siemens evidence", max_results=1),),
    )
    snapshot, event = RunStateGraph.accept_plan(snapshot, plan)
    events.append(event)
    snapshot, event = RunStateGraph.start_search(snapshot)
    events.append(event)
    hit = SearchHit(
        title="Siemens source",
        url=AnyHttpUrl("https://example.com/source"),
        snippet="Public evidence",
        rank=1,
    )
    evidence = ExtractedEvidence(
        evidence_id="ev-source",
        source_url=AnyHttpUrl("https://example.com/source"),
        source_title="Siemens source",
        summary="The source supports the answer.",
    )
    snapshot, event = RunStateGraph.record_evidence(
        snapshot,
        hits=(hit,),
        evidence=(evidence,),
    )
    events.append(event)
    answer = ScopedAnswer(
        answer_text="The documented answer is supported by the cited source.",
        citations=(
            Citation(
                claim="The source supports the answer.",
                evidence_id="ev-source",
                source_url=AnyHttpUrl("https://example.com/source"),
            ),
        ),
    )
    snapshot, event = RunStateGraph.draft_answer(snapshot, answer)
    events.append(event)
    snapshot, event = RunStateGraph.complete(snapshot)
    events.append(event)
    return RunResult(snapshot=snapshot, events=tuple(events), usage=usage())


def failed_result(*, reason: FailureReason) -> RunResult:
    snapshot = RunStateGraph.create(
        "tenant-one",
        "session-one",
        "run-one",
        "find the documented answer",
    )
    events = [_created(snapshot)]
    snapshot, event = RunStateGraph.fail(snapshot, reason)
    events.append(event)
    return RunResult(snapshot=snapshot, events=tuple(events), usage=usage())


def cancelled_result() -> RunResult:
    snapshot = RunStateGraph.create(
        "tenant-one",
        "session-one",
        "run-one",
        "find the documented answer",
    )
    events = [_created(snapshot)]
    snapshot, event = RunStateGraph.cancel(snapshot)
    events.append(event)
    return RunResult(snapshot=snapshot, events=tuple(events), usage=usage())


def usage() -> RunUsage:
    return RunUsage(
        elapsed_seconds=0.1,
        iterations=1,
        search_queries=0,
        pages=0,
        failed_pages=0,
        raw_bytes_reserved=0,
        decoded_bytes=0,
        model_calls=0,
        model_attempts=0,
        tokens=0,
    )


def _copy_run(run: RunRecord, **updates: object) -> RunRecord:
    return RunRecord.model_validate(
        run.model_copy(update={"version": run.version + 1, **updates}).model_dump(
            mode="python"
        )
    )


def _created(snapshot: RunSnapshot) -> PublicEvent:
    return PublicEvent(
        tenant_id=snapshot.tenant_id,
        session_id=snapshot.session_id,
        run_id=snapshot.run_id,
        event_type=EventType.RUN_CREATED,
        message="Created bounded research run",
    )
