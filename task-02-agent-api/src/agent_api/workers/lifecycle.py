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

    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("worker lifespan requires an owning task")
    stopping = False
    worker_ended = False

    def notify_owner(_: asyncio.Task[None]) -> None:
        nonlocal worker_ended
        if not stopping:
            worker_ended = True
            owner.cancel()

    task = asyncio.create_task(worker.run_forever())
    task.add_done_callback(notify_owner)
    try:
        try:
            yield
        except asyncio.CancelledError:
            if not worker_ended:
                raise
            owner.uncancel()
    finally:
        unexpected_exit = worker_ended or task.done()
        stopping = True
        worker.stop()
        deadline = asyncio.get_running_loop().time() + shutdown_seconds
        done, _ = await asyncio.wait(
            {task},
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
        if task not in done:
            # LocalWorker aborts its executor and discards late results outside the
            # repository boundary; visibility and lease expiry permit recovery.
            task.cancel()
            task.add_done_callback(_consume_task_result)
            await asyncio.sleep(0)
            if not task.done():
                await asyncio.sleep(0)
        else:
            task.result()
            if unexpected_exit:
                raise RuntimeError("managed worker exited unexpectedly")


def _consume_task_result(task: asyncio.Task[None]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


__all__ = ["ManagedWorker", "worker_lifespan"]
