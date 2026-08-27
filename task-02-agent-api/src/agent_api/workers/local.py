"""Local durable worker built on queue visibility and repository leases."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import math
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from search_agent import FailureReason, RunResult, RunUsage, TerminalState
from search_agent.contracts import OpaqueId, QueryText, ScopedAnswer
from search_agent.memory import RunReflection, reflect_run

from ..observability import OperationalTelemetry
from ..ports import (
    TERMINAL_RUN_STATES,
    ClaimDisposition,
    ClaimRequest,
    LeaseDisposition,
    LeaseRenewal,
    RunFailureCode,
    RunRecord,
    RunRepository,
    RunState,
    StateUpdate,
    WorkItem,
    WriteDisposition,
)
from ..schemas import validate_public_answer
from ..security.limits import ExecutionPermit, QuotaLimiter


class QueueReceiver(Protocol):
    async def receive(
        self, *, now: datetime, visibility_seconds: int
    ) -> WorkItem | None: ...

    async def cancel(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> int: ...


class RunExecutor(Protocol):
    async def run(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        request: QueryText,
    ) -> RunResult: ...


_FAILURE_CODES = {
    FailureReason.BUDGET_EXHAUSTED: RunFailureCode.BUDGET_EXHAUSTED,
    FailureReason.NO_EVIDENCE: RunFailureCode.NO_EVIDENCE,
    FailureReason.SEARCH_FAILED: RunFailureCode.SEARCH_FAILED,
    FailureReason.VALIDATION_FAILED: RunFailureCode.VALIDATION_FAILED,
}


@dataclass(frozen=True, slots=True)
class _TerminalProjection:
    next_state: RunState
    answer: ScopedAnswer | None = None
    failure_code: RunFailureCode | None = None
    reflection: RunReflection | None = None


class LocalWorker:
    def __init__(
        self,
        *,
        repository: RunRepository,
        queue: QueueReceiver,
        executor: RunExecutor,
        worker_id: OpaqueId,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lease_seconds: int = 30,
        visibility_seconds: int = 30,
        heartbeat_seconds: float = 10.0,
        cancellation_drain_seconds: float = 1.0,
        lease_id_factory: Callable[[], str] | None = None,
        limiter: QuotaLimiter | None = None,
        telemetry: OperationalTelemetry | None = None,
    ) -> None:
        if not 1 <= lease_seconds <= 900:
            raise ValueError("lease_seconds must be between 1 and 900")
        if not 1 <= visibility_seconds <= 900:
            raise ValueError("visibility_seconds must be between 1 and 900")
        if (
            isinstance(heartbeat_seconds, bool)
            or not isinstance(heartbeat_seconds, int | float)
            or heartbeat_seconds <= 0
            or heartbeat_seconds >= lease_seconds
        ):
            raise ValueError(
                "heartbeat_seconds must be positive and below lease_seconds"
            )
        if (
            isinstance(cancellation_drain_seconds, bool)
            or not isinstance(cancellation_drain_seconds, int | float)
            or not math.isfinite(cancellation_drain_seconds)
            or cancellation_drain_seconds <= 0
        ):
            raise ValueError("cancellation drain must be a positive finite number")
        self._repository = repository
        self._queue = queue
        self._executor = executor
        self._worker_id = worker_id
        self._clock = _utc_now if clock is None else clock
        self._sleep = sleep
        self._lease_seconds = lease_seconds
        self._visibility_seconds = visibility_seconds
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._cancellation_drain_seconds = float(cancellation_drain_seconds)
        self._lease_id_factory = (
            _new_lease_id if lease_id_factory is None else lease_id_factory
        )
        self._stop = asyncio.Event()
        self._limiter = limiter
        self._telemetry = telemetry

    async def process_one(self) -> bool:
        item = await self._queue.receive(
            now=self._clock(),
            visibility_seconds=self._visibility_seconds,
        )
        if item is None:
            return False
        await self.process(item)
        return True

    async def process(self, item: WorkItem) -> bool:
        permit: ExecutionPermit | None = None
        if self._limiter is not None:
            permit = await self._limiter.acquire_execution(
                tenant_id=item.tenant_id,
                run_id=item.run_id,
                at=self._clock(),
                lease_seconds=self._lease_seconds,
            )
            if permit is None:
                self._observe_lease(item, "blocked")
                self._observe_work(item, "quota_blocked")
                return False
            self._observe_lease(item, "acquired")
        try:
            return await self._process_admitted(item, permit)
        finally:
            if permit is not None:
                assert self._limiter is not None
                await self._limiter.release_execution(permit)
                self._observe_lease(item, "released")

    async def _process_admitted(
        self, item: WorkItem, permit: ExecutionPermit | None
    ) -> bool:
        claim = await self._repository.claim(
            ClaimRequest(
                tenant_id=item.tenant_id,
                run_id=item.run_id,
                worker_id=self._worker_id,
                lease_id=self._lease_id_factory(),
                now=self._clock(),
                lease_seconds=self._lease_seconds,
            )
        )
        run = claim.run
        if claim.disposition in {
            ClaimDisposition.TERMINAL,
            ClaimDisposition.NOT_FOUND,
        }:
            self._observe_work(
                item,
                "terminal"
                if claim.disposition is ClaimDisposition.TERMINAL
                else "not_found",
            )
            await self._queue.cancel(tenant_id=item.tenant_id, run_id=item.run_id)
            return True
        if claim.disposition is ClaimDisposition.CANCELLATION_REQUESTED:
            self._observe_work(item, "cancelled")
            if run is not None and run.state in TERMINAL_RUN_STATES:
                await self._observe_terminal(run, usage=None)
                await self._queue.cancel(tenant_id=item.tenant_id, run_id=item.run_id)
                return True
            return False
        if claim.disposition in {
            ClaimDisposition.ALREADY_CLAIMED,
            ClaimDisposition.BUSY,
            ClaimDisposition.LEASE_UNAVAILABLE,
        }:
            self._observe_work(item, "busy")
            return False
        assert run is not None
        self._observe_work(item, "claimed")
        return await self._execute(item, run, permit)

    async def run_forever(self, *, poll_interval: float = 1.0) -> None:
        while not self._stop.is_set():
            if await self.process_one():
                continue
            await self._sleep(poll_interval)

    def stop(self) -> None:
        self._stop.set()

    async def _execute(
        self, item: WorkItem, run: RunRecord, permit: ExecutionPermit | None
    ) -> bool:
        task = asyncio.create_task(
            self._executor.run(
                tenant_id=run.tenant_id,
                session_id=run.session_id,
                run_id=run.run_id,
                request=run.query,
            )
        )
        current = run
        try:
            while True:
                done, _ = await asyncio.wait(
                    {task},
                    timeout=self._heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break
                lease = await self._repository.renew_lease(
                    LeaseRenewal(
                        tenant_id=current.tenant_id,
                        run_id=current.run_id,
                        worker_id=self._worker_id,
                        lease_id=_lease_id(current),
                        now=self._clock(),
                        lease_seconds=self._lease_seconds,
                    )
                )
                if lease.disposition is LeaseDisposition.RENEWED:
                    assert lease.run is not None
                    if (
                        self._limiter is not None
                        and permit is not None
                        and not await self._limiter.renew_execution(
                            permit,
                            at=self._clock(),
                            lease_seconds=self._lease_seconds,
                        )
                    ):
                        self._observe_lease(item, "lost")
                        task.cancel()
                        await _drain_cancelled(
                            task, timeout=self._cancellation_drain_seconds
                        )
                        return False
                    self._observe_lease(item, "renewed")
                    current = lease.run
                    continue
                if lease.disposition is LeaseDisposition.CANCELLATION_REQUESTED:
                    self._observe_work(item, "cancelled")
                    task.cancel()
                    await _drain_cancelled(
                        task, timeout=self._cancellation_drain_seconds
                    )
                    if lease.run is not None:
                        await self._finish_cancellation(item, lease.run)
                    return True
                self._observe_lease(item, "lost")
                task.cancel()
                await _drain_cancelled(task, timeout=self._cancellation_drain_seconds)
                if lease.run is not None and lease.run.state in TERMINAL_RUN_STATES:
                    await self._queue.cancel(
                        tenant_id=item.tenant_id, run_id=item.run_id
                    )
                    return True
                return False
        except asyncio.CancelledError:
            task.cancel()
            await _drain_cancelled(task, timeout=self._cancellation_drain_seconds)
            raise
        except Exception:
            task.cancel()
            await _drain_cancelled(task, timeout=self._cancellation_drain_seconds)
            raise
        try:
            result = await task
        except asyncio.CancelledError:
            # Preserve supervisor cancellation while containing executor self-cancellation.
            worker_task = asyncio.current_task()
            if worker_task is not None and worker_task.cancelling():
                raise
            terminal = await self._finish_terminal(
                item,
                current,
                _TerminalProjection(
                    next_state=RunState.FAILED,
                    failure_code=RunFailureCode.EXECUTION_FAILED,
                ),
                usage=None,
            )
            if terminal is not None:
                self._observe_work(item, "failed")
            return terminal is not None
        except Exception:
            terminal = await self._finish_terminal(
                item,
                current,
                _TerminalProjection(
                    next_state=RunState.FAILED,
                    failure_code=RunFailureCode.EXECUTION_FAILED,
                ),
                usage=None,
            )
            if terminal is not None:
                self._observe_work(item, "failed")
            return terminal is not None
        try:
            projection = _projection_from_result(result, run=current)
            result_usage: RunUsage | None = result.usage
        except (AttributeError, TypeError, ValueError):
            projection = _TerminalProjection(
                next_state=RunState.FAILED,
                failure_code=RunFailureCode.EXECUTION_FAILED,
            )
            result_usage = None
        terminal = await self._finish_result(
            item, current, projection, usage=result_usage
        )
        if terminal is not None:
            self._observe_work(item, terminal.state.value)
        return terminal is not None

    async def _finish_result(
        self,
        item: WorkItem,
        run: RunRecord,
        projection: _TerminalProjection,
        *,
        usage: RunUsage | None,
    ) -> RunRecord | None:
        current = await self._repository.get(tenant_id=run.tenant_id, run_id=run.run_id)
        if current is None:
            await self._queue.cancel(tenant_id=item.tenant_id, run_id=item.run_id)
            return None
        if current.state in TERMINAL_RUN_STATES:
            await self._queue.cancel(tenant_id=item.tenant_id, run_id=item.run_id)
            return None
        if current.cancellation_requested_at is not None:
            await self._finish_cancellation(item, current)
            return None
        if not _owns(current, worker_id=self._worker_id, lease_id=_lease_id(run)):
            return None
        return await self._finish_terminal(item, current, projection, usage=usage)

    async def _finish_cancellation(self, item: WorkItem, run: RunRecord) -> None:
        if run.state in TERMINAL_RUN_STATES:
            await self._queue.cancel(tenant_id=item.tenant_id, run_id=item.run_id)
            return
        if not _owns(run, worker_id=self._worker_id, lease_id=_lease_id(run)):
            return
        write = await self._repository.compare_and_set(
            StateUpdate(
                tenant_id=run.tenant_id,
                run_id=run.run_id,
                expected_version=run.version,
                expected_state=run.state,
                next_state=RunState.CANCELLED,
                at=_transition_time(self._clock(), run.updated_at),
                lease_id=_lease_id(run),
                worker_id=self._worker_id,
            )
        )
        if write.disposition is WriteDisposition.APPLIED:
            assert write.run is not None
            await self._observe_terminal(write.run, usage=None)
            await self._queue.cancel(tenant_id=item.tenant_id, run_id=item.run_id)
        elif write.run is not None and write.run.state in TERMINAL_RUN_STATES:
            await self._queue.cancel(tenant_id=item.tenant_id, run_id=item.run_id)

    async def _finish_terminal(
        self,
        item: WorkItem,
        run: RunRecord,
        projection: _TerminalProjection,
        *,
        usage: RunUsage | None,
    ) -> RunRecord | None:
        write = await self._repository.compare_and_set(
            StateUpdate(
                tenant_id=run.tenant_id,
                run_id=run.run_id,
                expected_version=run.version,
                expected_state=run.state,
                next_state=projection.next_state,
                at=_transition_time(self._clock(), run.updated_at),
                lease_id=_lease_id(run),
                worker_id=self._worker_id,
                answer=projection.answer,
                failure_code=projection.failure_code,
                reflection=projection.reflection,
            )
        )
        if write.disposition is WriteDisposition.APPLIED:
            assert write.run is not None
            await self._observe_terminal(write.run, usage=usage)
            await self._queue.cancel(tenant_id=item.tenant_id, run_id=item.run_id)
            return write.run
        if (
            write.run is not None
            and write.run.cancellation_requested_at is not None
            and _owns(
                write.run,
                worker_id=self._worker_id,
                lease_id=_lease_id(run),
            )
        ):
            await self._finish_cancellation(item, write.run)
            return None
        if write.run is not None and write.run.state in TERMINAL_RUN_STATES:
            await self._queue.cancel(tenant_id=item.tenant_id, run_id=item.run_id)
        return None

    def _observe_work(self, item: WorkItem, outcome: str) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.work_outcome(
                tenant_id=item.tenant_id,
                run_id=item.run_id,
                outcome=outcome,
                at=self._clock(),
            )
        except Exception:
            return

    def _observe_lease(self, item: WorkItem, outcome: str) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.lease_outcome(
                tenant_id=item.tenant_id,
                run_id=item.run_id,
                outcome=outcome,
                at=self._clock(),
            )
        except Exception:
            return

    async def _observe_terminal(
        self, run: RunRecord, *, usage: RunUsage | None
    ) -> None:
        if self._telemetry is None:
            return
        try:
            await self._telemetry.run_terminal(
                tenant_id=run.tenant_id,
                session_id=run.session_id,
                run_id=run.run_id,
                state=run.state,
                failure_code=run.failure_code,
                usage=usage,
                at=run.updated_at,
            )
        except Exception:
            return


def _projection_from_result(
    result: RunResult, *, run: RunRecord
) -> _TerminalProjection:
    snapshot = result.snapshot
    if (
        snapshot.tenant_id,
        snapshot.session_id,
        snapshot.run_id,
        snapshot.request,
    ) != (run.tenant_id, run.session_id, run.run_id, run.query):
        raise ValueError("executor result does not match the submitted run")
    if snapshot.terminal_state is TerminalState.COMPLETED:
        if snapshot.answer is None:
            raise ValueError("completed result omitted its answer")
        return _TerminalProjection(
            next_state=RunState.COMPLETED,
            answer=validate_public_answer(snapshot.answer),
            reflection=reflect_run(result),
        )
    if snapshot.terminal_state is TerminalState.CANCELLED:
        return _TerminalProjection(
            next_state=RunState.CANCELLED,
            reflection=reflect_run(result),
        )
    if snapshot.failure_reason is None:
        raise ValueError("failed result omitted its failure reason")
    return _TerminalProjection(
        next_state=RunState.FAILED,
        failure_code=_FAILURE_CODES.get(
            snapshot.failure_reason,
            RunFailureCode.EXECUTION_FAILED,
        ),
        reflection=reflect_run(result),
    )


def _owns(run: RunRecord, *, worker_id: str, lease_id: str) -> bool:
    lease = run.lease
    return (
        lease is not None
        and lease.worker_id == worker_id
        and lease.lease_id == lease_id
    )


def _lease_id(run: RunRecord) -> str:
    assert run.lease is not None
    return run.lease.lease_id


def _transition_time(now: datetime, updated_at: datetime) -> datetime:
    return updated_at if now < updated_at else now


async def _drain_cancelled(task: asyncio.Task[RunResult], *, timeout: float) -> None:
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        # Repository writes stay in LocalWorker, so this detached executor's late
        # result is discarded while the durable lease expires for a new owner.
        task.add_done_callback(_consume_task_result)
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


def _consume_task_result(task: asyncio.Task[RunResult]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


def _new_lease_id() -> str:
    encoded = base64.b32encode(secrets.token_bytes(10)).decode().lower().rstrip("=")
    return f"lease-{encoded}"


def _utc_now() -> datetime:
    return datetime.now(UTC)
