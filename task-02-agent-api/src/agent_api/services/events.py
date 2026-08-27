"""Bounded resumable delivery of public run events."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Protocol

from search_agent.contracts import OpaqueId

from ..ports import TERMINAL_RUN_STATES, RunRepository
from ..schemas import SSE_HEARTBEAT, RunEvent, RunEventType, encode_sse
from ..storage import StorageError
from .runs import RunNotFound

_EVENT_BATCH_SIZE = 100


class _EventReader(Protocol):
    async def list(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[RunEvent, ...]: ...


class EventStreamService:
    """Open a tenant-owned event stream without buffering ahead of its consumer."""

    def __init__(
        self,
        run_repository: RunRepository,
        event_repository: _EventReader,
        *,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_seconds: float = 0.25,
        heartbeat_seconds: float = 15.0,
    ) -> None:
        for interval in (poll_seconds, heartbeat_seconds):
            if (
                isinstance(interval, bool)
                or not isinstance(interval, int | float)
                or not math.isfinite(interval)
                or interval <= 0
            ):
                raise ValueError("stream intervals must be positive and finite")
        self._runs = run_repository
        self._events = event_repository
        self._clock = clock
        self._sleep = sleep
        self._poll_seconds = float(poll_seconds)
        self._heartbeat_seconds = float(heartbeat_seconds)

    async def open_stream(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        after_sequence: int,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[bytes]:
        """Authorize and validate the first batch before HTTP streaming starts."""

        run = await self._runs.get(tenant_id=tenant_id, run_id=run_id)
        if run is None:
            raise RunNotFound
        initially_disconnected = await disconnected()
        initial = (
            ()
            if initially_disconnected
            else await self._read_batch(
                tenant_id=tenant_id,
                run_id=run_id,
                after_sequence=after_sequence,
            )
        )
        return self._stream(
            tenant_id=tenant_id,
            run_id=run_id,
            after_sequence=after_sequence,
            disconnected=disconnected,
            initial=initial,
            initially_complete=(
                initially_disconnected
                or (not initial and run.state in TERMINAL_RUN_STATES)
            ),
        )

    def now(self) -> datetime:
        return self._clock()

    async def _stream(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        after_sequence: int,
        disconnected: Callable[[], Awaitable[bool]],
        initial: tuple[RunEvent, ...],
        initially_complete: bool,
    ) -> AsyncIterator[bytes]:
        if initially_complete:
            return
        cursor = after_sequence
        batch = initial
        last_emission = self._clock()
        while True:
            for event in batch:
                if await disconnected():
                    return
                yield encode_sse(event)
                cursor = event.sequence
                last_emission = self._clock()
                if event.event_type is not RunEventType.STATUS:
                    return

            if await disconnected():
                return
            if not batch:
                now = self._clock()
                if (now - last_emission).total_seconds() >= self._heartbeat_seconds:
                    if await disconnected():
                        return
                    yield SSE_HEARTBEAT
                    last_emission = self._clock()
                if await disconnected():
                    return
                await self._sleep(self._poll_seconds)
            if await disconnected():
                return
            try:
                batch = await self._read_batch(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    after_sequence=cursor,
                )
            except StorageError:
                # Headers may already be sent, so terminate without exposing a
                # corrupted payload or private storage diagnostic in the SSE body.
                return
            if not batch:
                try:
                    run = await self._runs.get(tenant_id=tenant_id, run_id=run_id)
                except StorageError:
                    return
                if run is None:
                    return
                if run.state in TERMINAL_RUN_STATES:
                    try:
                        batch = await self._read_batch(
                            tenant_id=tenant_id,
                            run_id=run_id,
                            after_sequence=cursor,
                        )
                    except StorageError:
                        return
                    if not batch:
                        return

    async def _read_batch(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        after_sequence: int,
    ) -> tuple[RunEvent, ...]:
        events = await self._events.list(
            tenant_id=tenant_id,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=_EVENT_BATCH_SIZE,
        )
        if type(events) is not tuple or len(events) > _EVENT_BATCH_SIZE:
            raise StorageError("stored run event batch is invalid")
        checked: list[RunEvent] = []
        cursor = after_sequence
        for event in events:
            try:
                if type(event) is not RunEvent:
                    raise ValueError("stored event has the wrong concrete type")
                public = RunEvent.model_validate(event.model_dump(mode="python"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise StorageError("stored run event is invalid") from exc
            if public.run_id != run_id or public.sequence <= cursor:
                raise StorageError("stored run event sequence is invalid")
            checked.append(public)
            cursor = public.sequence
        return tuple(checked)


__all__ = ["EventStreamService"]
