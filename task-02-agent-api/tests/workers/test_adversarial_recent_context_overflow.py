from __future__ import annotations

from datetime import timedelta
from typing import cast

import pytest
from workers.test_local import (
    NOW,
    ContextExecutor,
    FakeQueue,
    MutableClock,
    SessionRunRepository,
    completed_result,
    queued_run,
    work_item,
)

from agent_api import RunRepository
from agent_api.ports import RunFailureCode, RunRecord, RunState
from agent_api.workers import LocalWorker
from search_agent import ScopedAnswer


class ProductionRecentWindowRepository(SessionRunRepository):
    async def list_session_recent(
        self, *, tenant_id: str, session_id: str, limit: int = 100
    ) -> tuple[RunRecord, ...]:
        async with self._lock:
            selected = [
                run
                for run in self._runs.values()
                if run.tenant_id == tenant_id and run.session_id == session_id
            ]
        selected.sort(key=lambda run: (run.created_at, run.run_id))
        return tuple(selected[-limit:])


@pytest.mark.asyncio
async def test_more_than_one_hundred_recent_failures_do_not_evict_completed_context() -> (
    None
):
    completed = [
        RunRecord(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id=f"run-completed-{index}",
            generation_id=f"generation-completed-{index}",
            idempotency_key=f"request-key-completed-{index}",
            query=f"Research completed Siemens topic {index}",
            state=RunState.COMPLETED,
            version=1,
            delivery_attempts=1,
            created_at=NOW - timedelta(minutes=300 - index),
            updated_at=NOW - timedelta(minutes=299 - index),
            terminal_at=NOW - timedelta(minutes=299 - index),
            answer=ScopedAnswer(
                answer_text=f"Completed Siemens answer {index}.",
                citations=(),
            ),
        )
        for index in range(6)
    ]
    failed = [
        RunRecord(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id=f"run-failed-{index}",
            generation_id=f"generation-failed-{index}",
            idempotency_key=f"request-key-failed-{index}",
            query=f"Failed Siemens topic {index}",
            state=RunState.FAILED,
            version=1,
            delivery_attempts=1,
            created_at=NOW - timedelta(minutes=200 - index),
            updated_at=NOW - timedelta(minutes=199 - index),
            terminal_at=NOW - timedelta(minutes=199 - index),
            failure_code=RunFailureCode.EXECUTION_FAILED,
        )
        for index in range(101)
    ]
    current = queued_run().model_copy(update={"created_at": NOW, "updated_at": NOW})
    repository = ProductionRecentWindowRepository(*completed, *failed, current)
    executor = ContextExecutor(result=completed_result())
    worker = LocalWorker(
        repository=cast(RunRepository, repository),
        queue=FakeQueue(work_item()),
        executor=executor,
        worker_id="worker-one",
        clock=MutableClock(NOW),
        lease_id_factory=lambda: "lease-one",
    )

    await worker._invoke_executor(current)

    assert [turn.request for turn in executor.contexts[0]] == [
        f"Research completed Siemens topic {index}" for index in range(6)
    ]
