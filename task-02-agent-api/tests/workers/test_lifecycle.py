from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_api.app import create_app
from agent_api.ports import RunRecord, RunState, RunSubmission, WorkItem
from agent_api.schemas import RunEventType
from agent_api.storage import (
    SessionRecord,
    SQLiteEventRepository,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    SQLiteWorkQueue,
    TenantRecord,
    migrate,
)
from agent_api.workers import worker_lifespan
from search_agent import RunResult

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass(slots=True)
class DrainingWorker:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False

    async def run_forever(self, *, poll_interval: float = 1.0) -> None:
        del poll_interval
        self.started.set()
        try:
            await self.stopping.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.finished.set()

    def stop(self) -> None:
        self.stopping.set()


@dataclass(slots=True)
class StubbornWorker:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    ignored_cancellation: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    async def run_forever(self, *, poll_interval: float = 1.0) -> None:
        del poll_interval
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.ignored_cancellation.set()
            await self.release.wait()
        finally:
            self.finished.set()

    def stop(self) -> None:
        pass


@dataclass(slots=True)
class BlockingExecutor:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(
        self,
        *,
        tenant_id: str,
        session_id: str,
        run_id: str,
        request: str,
    ) -> RunResult:
        del tenant_id, session_id, run_id, request
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("blocked executor unexpectedly resumed")


@dataclass(slots=True)
class FailingExecutor:
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(
        self,
        *,
        tenant_id: str,
        session_id: str,
        run_id: str,
        request: str,
    ) -> RunResult:
        del tenant_id, session_id, run_id, request
        self.started.set()
        raise RuntimeError("simulated private executor failure")


@dataclass(slots=True)
class SuppressingExecutor:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    ignored_cancellation: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(
        self,
        *,
        tenant_id: str,
        session_id: str,
        run_id: str,
        request: str,
    ) -> RunResult:
        del tenant_id, session_id, run_id, request
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.ignored_cancellation.set()
            await self.release.wait()
        finally:
            self.finished.set()
        raise RuntimeError("ignored cancellation result must be discarded")


@pytest.mark.asyncio
async def test_worker_lifespan_gracefully_drains_after_stop() -> None:
    worker = DrainingWorker()

    async with worker_lifespan(worker, shutdown_seconds=0.1):
        await asyncio.wait_for(worker.started.wait(), timeout=1)

    assert worker.finished.is_set()
    assert worker.cancelled is False


@pytest.mark.asyncio
async def test_worker_lifespan_remains_bounded_if_cancellation_is_suppressed() -> None:
    worker = StubbornWorker()
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    async with worker_lifespan(worker, shutdown_seconds=0.01):
        await asyncio.wait_for(worker.started.wait(), timeout=1)

    assert loop.time() - started_at < 0.2
    assert worker.ignored_cancellation.is_set()
    worker.release.set()
    await asyncio.wait_for(worker.finished.wait(), timeout=1)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan"), True])
@pytest.mark.asyncio
async def test_worker_lifespan_rejects_unbounded_timeouts(timeout: float) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        async with worker_lifespan(None, shutdown_seconds=timeout):
            pass


@pytest.mark.asyncio
async def test_forced_app_shutdown_leaves_work_recoverable_after_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shutdown-restart.sqlite3"
    await seed_work(database_path)
    clock = MutableClock(NOW)
    blocked = BlockingExecutor()
    first = create_app(
        database_path=database_path,
        pepper_provider=FixedPepper(),
        clock=clock,
        run_executor=blocked,
        worker_shutdown_seconds=0.01,
    )

    async with first.router.lifespan_context(first):
        await asyncio.wait_for(blocked.started.wait(), timeout=2)

    stored = await SQLiteRunRepository(database_path).get(
        tenant_id="tenant-one", run_id="run-one"
    )
    assert blocked.cancelled.is_set()
    assert stored is not None and stored.state is RunState.RUNNING
    assert stored.lease is not None and stored.terminal_at is None

    clock.advance(seconds=30)
    recovery = FailingExecutor()
    second = create_app(
        database_path=database_path,
        pepper_provider=FixedPepper(),
        clock=clock,
        run_executor=recovery,
        worker_shutdown_seconds=0.05,
    )
    async with second.router.lifespan_context(second):
        await asyncio.wait_for(recovery.started.wait(), timeout=2)
        recovered = await wait_for_state(
            database_path,
            state=RunState.FAILED,
        )

    events = await SQLiteEventRepository(database_path).list(
        tenant_id="tenant-one", run_id="run-one"
    )
    assert recovered.delivery_attempts == 2
    assert recovered.failure_code is not None
    assert [event.event_type for event in events].count(RunEventType.FAILED) == 1


@pytest.mark.asyncio
async def test_app_shutdown_is_bounded_when_executor_suppresses_cancellation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "suppressed-cancellation.sqlite3"
    await seed_work(database_path)
    executor = SuppressingExecutor()
    app = create_app(
        database_path=database_path,
        pepper_provider=FixedPepper(),
        clock=lambda: NOW,
        run_executor=executor,
        worker_shutdown_seconds=0.01,
    )
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(executor.started.wait(), timeout=2)

    assert loop.time() - started_at < 0.2
    assert executor.ignored_cancellation.is_set()
    stored = await SQLiteRunRepository(database_path).get(
        tenant_id="tenant-one", run_id="run-one"
    )
    events = await SQLiteEventRepository(database_path).list(
        tenant_id="tenant-one", run_id="run-one"
    )
    assert stored is not None and stored.state is RunState.RUNNING
    assert not any(
        event.event_type
        in {
            RunEventType.COMPLETED,
            RunEventType.FAILED,
            RunEventType.CANCELLED,
            RunEventType.EXPIRED,
        }
        for event in events
    )

    executor.release.set()
    await asyncio.wait_for(executor.finished.wait(), timeout=1)


async def seed_work(database_path: Path) -> None:
    await migrate(database_path)
    await SQLiteTenantRepository(database_path).put(
        TenantRecord(tenant_id="tenant-one", created_at=NOW)
    )
    await SQLiteSessionRepository(database_path).put(
        SessionRecord(
            tenant_id="tenant-one",
            session_id="session-one",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await SQLiteRunRepository(database_path).create(
        RunSubmission(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-one",
            idempotency_key="request-key-one",
            query="Find the documented answer.",
            created_at=NOW,
        )
    )
    await SQLiteWorkQueue(database_path).enqueue(
        WorkItem(
            work_id="work-one",
            tenant_id="tenant-one",
            run_id="run-one",
            enqueued_at=NOW,
            not_before=NOW,
        )
    )


async def wait_for_state(
    database_path: Path,
    *,
    state: RunState,
) -> RunRecord:
    repository = SQLiteRunRepository(database_path)
    for _ in range(100):
        run = await repository.get(tenant_id="tenant-one", run_id="run-one")
        if run is not None and run.state is state:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"run did not reach {state.value}")
