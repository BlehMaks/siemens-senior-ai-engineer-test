"""Bounded application lifecycle for the local durable worker."""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol


class ManagedWorker(Protocol):
    async def run_forever(self, *, poll_interval: float = 1.0) -> None: ...

    def stop(self) -> None: ...


@asynccontextmanager
async def worker_lifespan(
    worker: ManagedWorker | None,
    *,
    shutdown_seconds: float,
) -> AsyncIterator[None]:
    """Drain completed work, then cancel overdue execution for lease recovery."""

    if (
        isinstance(shutdown_seconds, bool)
        or not isinstance(shutdown_seconds, int | float)
        or not math.isfinite(shutdown_seconds)
        or shutdown_seconds <= 0
    ):
        raise ValueError("worker shutdown timeout must be a positive finite number")
    if worker is None:
        yield
        return

    task = asyncio.create_task(worker.run_forever())
    try:
        yield
    finally:
        worker.stop()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=shutdown_seconds)
        except TimeoutError:
            # LocalWorker propagates this cancellation after aborting its executor.
            # Durable visibility and the expiring lease then permit restart recovery.
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=shutdown_seconds)
            if task not in done:
                task.add_done_callback(_consume_task_result)
            else:
                with contextlib.suppress(asyncio.CancelledError):
                    task.result()


def _consume_task_result(task: asyncio.Task[None]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


__all__ = ["ManagedWorker", "worker_lifespan"]
