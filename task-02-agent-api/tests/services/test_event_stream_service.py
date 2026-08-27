from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from agent_api.ports import RunRepository, RunState
from agent_api.schemas import SSE_HEARTBEAT, RunEvent, RunEventType
from agent_api.services import EventStreamService, RunNotFound
from agent_api.storage import StorageError

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class RunReader:
    def __init__(
        self,
        existing: set[tuple[str, str]],
        *,
        state: RunState = RunState.RUNNING,
    ) -> None:
        self.existing = existing
        self.state = state

    async def get(self, *, tenant_id: str, run_id: str) -> object | None:
        if (tenant_id, run_id) not in self.existing:
            return None
        return SimpleNamespace(state=self.state)


class MutableRunReader(RunReader):
    def __init__(self) -> None:
        super().__init__({("tenant-one", "run-one")})
        self.get_calls = 0

    async def get(self, *, tenant_id: str, run_id: str) -> object | None:
        self.get_calls += 1
        return await super().get(tenant_id=tenant_id, run_id=run_id)


class EventReader:
    def __init__(self, events: tuple[RunEvent, ...]) -> None:
        self.events = events
        self.calls: list[tuple[str, str, int, int]] = []

    async def list(
        self,
        *,
        tenant_id: str,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[RunEvent, ...]:
        self.calls.append((tenant_id, run_id, after_sequence, limit))
        return tuple(event for event in self.events if event.sequence > after_sequence)[
            :limit
        ]


class FailAfterFirstBatch(EventReader):
    async def list(
        self,
        *,
        tenant_id: str,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[RunEvent, ...]:
        if self.calls:
            raise StorageError("private corrupted payload detail")
        return await super().list(
            tenant_id=tenant_id,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
        )


async def connected() -> bool:
    return False


def fixed_clock() -> datetime:
    return NOW


def event(sequence: int, *, terminal: bool = False) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        run_id="run-one",
        event_type=(RunEventType.CANCELLED if terminal else RunEventType.STATUS),
        state=(RunState.CANCELLED if terminal else RunState.RUNNING),
        occurred_at=NOW + timedelta(seconds=sequence),
        message="Run was cancelled." if terminal else "Run is running.",
    )


def service(
    reader: EventReader,
    *,
    clock: Callable[[], datetime] = fixed_clock,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    poll_seconds: float = 0.25,
    heartbeat_seconds: float = 15.0,
) -> EventStreamService:
    return EventStreamService(
        cast(RunRepository, RunReader({("tenant-one", "run-one")})),
        reader,
        clock=clock,
        sleep=asyncio.sleep if sleep is None else sleep,
        poll_seconds=poll_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )


async def collect(stream: AsyncIterator[bytes]) -> tuple[bytes, ...]:
    return tuple([frame async for frame in stream])


@pytest.mark.asyncio
async def test_resume_reads_strictly_after_cursor_and_terminal_event_closes() -> None:
    reader = EventReader((event(1), event(2), event(3, terminal=True)))
    stream = await service(reader).open_stream(
        tenant_id="tenant-one",
        run_id="run-one",
        after_sequence=1,
        disconnected=connected,
    )

    frames = await collect(stream)

    assert [frame.splitlines()[0] for frame in frames] == [b"id: 2", b"id: 3"]
    assert reader.calls == [("tenant-one", "run-one", 1, 100)]


@pytest.mark.asyncio
async def test_resume_at_terminal_sequence_closes_without_heartbeat() -> None:
    reader = EventReader((event(1), event(2, terminal=True)))
    subject = EventStreamService(
        cast(
            RunRepository,
            RunReader(
                {("tenant-one", "run-one")},
                state=RunState.CANCELLED,
            ),
        ),
        reader,
        clock=lambda: NOW,
    )
    stream = await subject.open_stream(
        tenant_id="tenant-one",
        run_id="run-one",
        after_sequence=2,
        disconnected=connected,
    )

    assert await collect(stream) == ()
    assert reader.calls == [("tenant-one", "run-one", 2, 100)]


@pytest.mark.asyncio
async def test_future_cursor_closes_after_run_becomes_terminal() -> None:
    runs = MutableRunReader()
    sleeps = 0

    async def sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            runs.state = RunState.CANCELLED
            return
        raise AssertionError("stream kept polling after terminal state")

    subject = EventStreamService(
        cast(RunRepository, runs),
        EventReader(()),
        clock=lambda: NOW,
        sleep=sleep,
        poll_seconds=0.01,
        heartbeat_seconds=1.0,
    )
    stream = await subject.open_stream(
        tenant_id="tenant-one",
        run_id="run-one",
        after_sequence=99,
        disconnected=connected,
    )

    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert runs.get_calls >= 2


@pytest.mark.asyncio
async def test_empty_stream_polls_at_bound_and_emits_heartbeat() -> None:
    current = NOW
    sleeps: list[float] = []

    def clock() -> datetime:
        return current

    async def sleep(seconds: float) -> None:
        nonlocal current
        sleeps.append(seconds)
        current += timedelta(seconds=seconds)

    reader = EventReader(())
    stream = cast(
        AsyncGenerator[bytes, None],
        await service(
            reader,
            clock=clock,
            sleep=sleep,
            poll_seconds=0.25,
            heartbeat_seconds=0.5,
        ).open_stream(
            tenant_id="tenant-one",
            run_id="run-one",
            after_sequence=0,
            disconnected=connected,
        ),
    )

    assert await anext(stream) == SSE_HEARTBEAT
    await stream.aclose()
    assert sleeps == [0.25, 0.25]
    assert all(call[3] == 100 for call in reader.calls)


@pytest.mark.asyncio
async def test_stream_obeys_consumer_backpressure_and_disconnect() -> None:
    disconnected = False

    async def connection_state() -> bool:
        return disconnected

    reader = EventReader((event(1), event(2)))
    stream = await service(reader).open_stream(
        tenant_id="tenant-one",
        run_id="run-one",
        after_sequence=0,
        disconnected=connection_state,
    )

    assert (await anext(stream)).startswith(b"id: 1\n")
    assert len(reader.calls) == 1
    disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert len(reader.calls) == 1


@pytest.mark.asyncio
async def test_later_storage_failure_closes_without_an_sse_error_payload() -> None:
    reader = FailAfterFirstBatch((event(1),))
    stream = await service(reader).open_stream(
        tenant_id="tenant-one",
        run_id="run-one",
        after_sequence=0,
        disconnected=connected,
    )

    assert (await anext(stream)).startswith(b"id: 1\n")
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert len(reader.calls) == 1


@pytest.mark.asyncio
async def test_missing_or_foreign_run_is_hidden_before_event_read() -> None:
    reader = EventReader((event(1, terminal=True),))
    subject = EventStreamService(
        cast(RunRepository, RunReader(set())),
        reader,
        clock=lambda: NOW,
    )

    with pytest.raises(RunNotFound):
        await subject.open_stream(
            tenant_id="tenant-foreign",
            run_id="run-one",
            after_sequence=0,
            disconnected=connected,
        )
    assert reader.calls == []


@pytest.mark.asyncio
async def test_corrupted_first_batch_fails_before_stream_is_returned() -> None:
    malformed = RunEvent.model_construct(
        schema_version=1,
        sequence=1,
        run_id="run-one",
        event_type=RunEventType.STATUS,
        state=RunState.COMPLETED,
        occurred_at=NOW,
        message="Invalid terminal status event.",
        answer=None,
        failure=None,
    )
    reader = EventReader((malformed,))

    with pytest.raises(StorageError, match="stored run event is invalid"):
        await service(reader).open_stream(
            tenant_id="tenant-one",
            run_id="run-one",
            after_sequence=0,
            disconnected=connected,
        )


@pytest.mark.parametrize(
    ("poll", "heartbeat"),
    [
        (0.0, 1.0),
        (0.1, 0.0),
        (float("nan"), 1.0),
        (0.1, float("inf")),
        (True, 1.0),
    ],
)
def test_stream_intervals_must_prevent_a_tight_loop(
    poll: float, heartbeat: float
) -> None:
    with pytest.raises(ValueError, match="intervals must be positive and finite"):
        service(
            EventReader(()),
            poll_seconds=poll,
            heartbeat_seconds=heartbeat,
        )
