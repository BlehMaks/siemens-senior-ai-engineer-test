from __future__ import annotations

import asyncio
from typing import Any

import pytest
from storage.test_cloud_adapters import (
    FakeCloudTaskClient,
    FakeDocumentStore,
    OptimisticDocumentStore,
    _seed_session,
)
from storage.test_run_generation import item, queue
from test_ports_contract import NOW, submission

from agent_api.services import RunService
from agent_api.storage import FirestoreRunRepository


class PausedCancellationRepository:
    def __init__(self, delegate: FirestoreRunRepository) -> None:
        self._delegate = delegate
        self.cancel_persisted = asyncio.Event()
        self.resume_cancel = asyncio.Event()

    async def request_cancellation(self, **kwargs: Any) -> Any:
        result = await self._delegate.request_cancellation(**kwargs)
        self.cancel_persisted.set()
        await self.resume_cancel.wait()
        return result


@pytest.mark.asyncio
async def test_duplicate_enqueue_does_not_contend_on_a_healthy_index() -> None:
    store = OptimisticDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    run = (await FirestoreRunRepository(store).create(submission())).run
    client = FakeCloudTaskClient()
    work_queue = queue(store, client)
    delivery = (await work_queue.enqueue(item(generation_id=run.generation_id))).item

    results = await asyncio.gather(
        *(work_queue.enqueue(delivery) for _ in range(18)),
        return_exceptions=True,
    )

    assert not [result for result in results if isinstance(result, BaseException)]
    assert len(await store.list(collection="work_items")) == 1
    assert len(client._tasks) == 1


@pytest.mark.asyncio
async def test_cancel_cleanup_is_scoped_to_the_persisted_generation() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    old_run = (await runs.create(submission())).run
    client = FakeCloudTaskClient()
    work_queue = queue(store, client)
    await work_queue.enqueue(item(generation_id=old_run.generation_id))
    paused_runs = PausedCancellationRepository(runs)
    service = RunService(paused_runs, work_queue, clock=lambda: NOW)
    pending = asyncio.create_task(
        service.cancel(tenant_id="tenant-one", run_id="run-one")
    )
    await paused_runs.cancel_persisted.wait()

    assert await runs.delete_run(tenant_id="tenant-one", run_id="run-one")
    replacement = (await runs.create(submission())).run
    await work_queue.enqueue(item(generation_id=replacement.generation_id))
    replacement_index = await store.get(collection="work_items", document_id="work-one")
    assert replacement_index is not None
    replacement_task_name = str(replacement_index["task_name"])

    paused_runs.resume_cancel.set()
    await pending

    assert (
        await store.get(collection="work_items", document_id="work-one")
        == replacement_index
    )
    assert await client.get(name=replacement_task_name) is not None


@pytest.mark.asyncio
async def test_late_delivery_cannot_discard_a_repaired_task() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    run = (await FirestoreRunRepository(store).create(submission())).run
    client = FakeCloudTaskClient()
    work_queue = queue(store, client)
    delivery = (await work_queue.enqueue(item(generation_id=run.generation_id))).item
    before = await store.get(collection="work_items", document_id=delivery.work_id)
    assert before is not None
    old_task_name = str(before["task_name"])

    assert await client.delete(name=old_task_name)
    assert not (await work_queue.enqueue(delivery)).created
    repaired = await store.get(collection="work_items", document_id=delivery.work_id)
    assert repaired is not None
    repaired_task_name = str(repaired["task_name"])
    assert repaired_task_name != old_task_name

    assert not await work_queue.discard(delivery)
    assert (
        await store.get(collection="work_items", document_id=delivery.work_id)
        == repaired
    )
    assert await client.get(name=repaired_task_name) is not None
