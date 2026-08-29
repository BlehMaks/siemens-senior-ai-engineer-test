from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from storage.test_cloud_adapters import (
    CompletedExecutor,
    FakeCloudTaskClient,
    FakeDocumentStore,
    ReadBeforeWriteTransaction,
    _seed_session,
)
from test_ports_contract import NOW, submission

from agent_api.ports import (
    RunParentNotFoundError,
    RunState,
    WorkItem,
)
from agent_api.storage import (
    CloudTask,
    CloudTasksWorkQueue,
    FirestoreRunRepository,
    SignedWorkItemCodec,
)
from agent_api.workers.local import LocalWorker

QUEUE_NAME = "projects/test/locations/eu/queues/dispatch"


class PausedTaskClient(FakeCloudTaskClient):
    def __init__(self) -> None:
        super().__init__()
        self.create_started = asyncio.Event()
        self.resume_create = asyncio.Event()
        self.create_calls = 0

    async def create(self, task: CloudTask) -> CloudTask:
        self.create_calls += 1
        created = await super().create(task)
        if self.create_calls == 1:
            self.create_started.set()
            await self.resume_create.wait()
        return created


class PausedDeleteTaskClient(FakeCloudTaskClient):
    def __init__(self) -> None:
        super().__init__()
        self.delete_started = asyncio.Event()
        self.resume_delete = asyncio.Event()
        self.delete_calls = 0

    async def delete(self, *, name: str) -> bool:
        self.delete_calls += 1
        deleted = await super().delete(name=name)
        if self.delete_calls == 1:
            self.delete_started.set()
            await self.resume_delete.wait()
        return deleted


class RetryingIdentityStore(FakeDocumentStore):
    def __init__(self) -> None:
        super().__init__()
        self.retry_replacement: dict[str, object] | None = None

    async def transaction(
        self,
        operation: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        replacement = self.retry_replacement
        if replacement is None:
            return await super().transaction(operation)
        self.retry_replacement = None
        async with self._lock:
            snapshot = copy.deepcopy(self._collections)
            await operation(ReadBeforeWriteTransaction(self))
            self._collections = snapshot
            await self.set(
                collection="work_items",
                document_id="work-one",
                document=replacement,
            )
            return await operation(ReadBeforeWriteTransaction(self))


def queue(store: FakeDocumentStore, client: FakeCloudTaskClient) -> CloudTasksWorkQueue:
    return CloudTasksWorkQueue(
        store=store,
        task_client=client,
        queue_name=QUEUE_NAME,
        codec=SignedWorkItemCodec(b"s" * 32),
    )


def item(*, generation_id: str | None = None) -> WorkItem:
    return WorkItem(
        work_id="work-one",
        tenant_id="tenant-one",
        run_id="run-one",
        generation_id=generation_id,
        enqueued_at=NOW,
        not_before=NOW,
    )


@pytest.mark.asyncio
async def test_enqueue_rejects_an_identically_recreated_run() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    first = await runs.create(submission())
    client = PausedTaskClient()
    pending = asyncio.create_task(queue(store, client).enqueue(item()))
    await client.create_started.wait()

    assert await runs.delete_run(tenant_id="tenant-one", run_id="run-one")
    replacement = await runs.create(submission())
    assert first.run.generation_id != replacement.run.generation_id
    client.resume_create.set()

    with pytest.raises(RunParentNotFoundError):
        await pending
    assert not client._tasks
    assert not await store.list(collection="work_items")


@pytest.mark.asyncio
async def test_enqueue_accepts_lifecycle_changes_to_the_same_run() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    created = await runs.create(submission())
    client = PausedTaskClient()
    pending = asyncio.create_task(queue(store, client).enqueue(item()))
    await client.create_started.wait()

    cancelled = await runs.request_cancellation(
        tenant_id="tenant-one",
        run_id="run-one",
        at=NOW + timedelta(seconds=1),
    )
    assert cancelled.run is not None
    assert cancelled.run.generation_id == created.run.generation_id
    client.resume_create.set()

    result = await pending
    assert result.item.generation_id == created.run.generation_id


@pytest.mark.asyncio
async def test_enqueue_isolates_an_orphaned_task_from_a_recreated_run() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    old_run = (await runs.create(submission())).run
    client = FakeCloudTaskClient()
    work_queue = queue(store, client)
    old_item = item(generation_id=old_run.generation_id)
    await work_queue.enqueue(old_item)
    assert await store.delete(collection="work_items", document_id="work-one")

    assert await runs.delete_run(tenant_id="tenant-one", run_id="run-one")
    replacement = (await runs.create(submission())).run

    enqueued = await work_queue.enqueue(item(generation_id=replacement.generation_id))

    assert enqueued.created
    assert len(client._tasks) == 2
    assert enqueued.item.generation_id == replacement.generation_id


@pytest.mark.asyncio
async def test_late_old_enqueue_cleanup_preserves_the_replacement_task() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    old_run = (await runs.create(submission())).run
    client = PausedTaskClient()
    work_queue = queue(store, client)
    old_enqueue = asyncio.create_task(
        work_queue.enqueue(item(generation_id=old_run.generation_id))
    )
    await client.create_started.wait()

    assert await runs.delete_run(tenant_id="tenant-one", run_id="run-one")
    replacement = (await runs.create(submission())).run
    current = await work_queue.enqueue(item(generation_id=replacement.generation_id))
    client.resume_create.set()

    with pytest.raises(RunParentNotFoundError):
        await old_enqueue
    assert current.created
    assert len(client._tasks) == 1
    remote = next(iter(client._tasks.values()))
    assert (
        work_queue.decode_delivery(
            body=remote.body,
            signature=dict(remote.headers)["X-Agent-Api-Task-Signature"],
            task_name=remote.name,
            queue_name=QUEUE_NAME,
        ).generation_id
        == replacement.generation_id
    )


@pytest.mark.asyncio
async def test_late_old_cancel_preserves_the_replacement_index() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    old_run = (await runs.create(submission())).run
    client = PausedDeleteTaskClient()
    work_queue = queue(store, client)
    await work_queue.enqueue(item(generation_id=old_run.generation_id))
    old_cancel = asyncio.create_task(
        work_queue.cancel(tenant_id="tenant-one", run_id="run-one")
    )
    await client.delete_started.wait()

    assert await runs.delete_run(tenant_id="tenant-one", run_id="run-one")
    replacement = (await runs.create(submission())).run
    await work_queue.enqueue(item(generation_id=replacement.generation_id))
    client.resume_delete.set()

    assert await old_cancel == 0
    assert await work_queue.cancel(tenant_id="tenant-one", run_id="run-one") == 1
    assert not client._tasks


@pytest.mark.asyncio
async def test_late_cancel_preserves_an_identical_reenqueue_index() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    run = (await FirestoreRunRepository(store).create(submission())).run
    client = PausedDeleteTaskClient()
    work_queue = queue(store, client)
    work = item(generation_id=run.generation_id)
    await work_queue.enqueue(work)
    first_cancel = asyncio.create_task(
        work_queue.cancel(tenant_id="tenant-one", run_id="run-one")
    )
    await client.delete_started.wait()

    assert await work_queue.cancel(tenant_id="tenant-one", run_id="run-one") == 1
    assert (await work_queue.enqueue(work)).created
    client.resume_delete.set()

    assert await first_cancel == 0
    assert len(client._tasks) == 1
    assert len(await store.list(collection="work_items")) == 1


@pytest.mark.asyncio
async def test_cancel_does_not_follow_a_corrupt_remote_task_name() -> None:
    store = FakeDocumentStore()
    client = FakeCloudTaskClient()
    work_queue = queue(store, client)
    runs = FirestoreRunRepository(store)
    for tenant_id in ("tenant-one", "tenant-two"):
        await _seed_session(store, tenant_id=tenant_id, session_id="session-one")
    await runs.create(submission())
    await runs.create(
        submission(
            tenant_id="tenant-two",
            run_id="run-two",
            idempotency_key="request-key-two",
        )
    )
    await work_queue.enqueue(item())
    await work_queue.enqueue(
        WorkItem(
            work_id="work-two",
            tenant_id="tenant-two",
            run_id="run-two",
            enqueued_at=NOW,
            not_before=NOW,
        )
    )
    rows = {row["work_id"]: row for row in await store.list(collection="work_items")}
    victim_task_name = rows["work-two"]["task_name"]
    corrupt = dict(rows["work-one"])
    corrupt["task_name"] = victim_task_name
    await store.set(
        collection="work_items",
        document_id="work-one",
        document=corrupt,
    )

    assert await work_queue.cancel(tenant_id="tenant-one", run_id="run-one") == 1
    assert victim_task_name in client._tasks
    assert await store.get(collection="work_items", document_id="work-one") is None


@pytest.mark.asyncio
async def test_cancel_preserves_indexes_when_physical_identity_is_ambiguous() -> None:
    store = FakeDocumentStore()
    client = FakeCloudTaskClient()
    work_queue = queue(store, client)
    runs = FirestoreRunRepository(store)
    for tenant_id in ("tenant-one", "tenant-two"):
        await _seed_session(store, tenant_id=tenant_id, session_id="session-one")
    await runs.create(submission())
    await runs.create(
        submission(
            tenant_id="tenant-two",
            run_id="run-two",
            idempotency_key="request-key-two",
        )
    )
    await work_queue.enqueue(item())
    await work_queue.enqueue(
        WorkItem(
            work_id="work-two",
            tenant_id="tenant-two",
            run_id="run-two",
            enqueued_at=NOW,
            not_before=NOW,
        )
    )
    victim = await store.get(collection="work_items", document_id="work-two")
    assert victim is not None
    corrupt = dict(victim)
    corrupt.update({"tenant_id": "tenant-one", "run_id": "run-one"})
    await store.set(
        collection="work_items",
        document_id="work-one",
        document=corrupt,
    )

    assert await work_queue.cancel(tenant_id="tenant-one", run_id="run-one") == 0
    assert await store.get(collection="work_items", document_id="work-two") == victim
    assert str(victim["task_name"]) in client._tasks


@pytest.mark.asyncio
async def test_cancel_rejects_an_inconsistent_stored_work_id() -> None:
    store = FakeDocumentStore()
    client = FakeCloudTaskClient()
    work_queue = queue(store, client)
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    await FirestoreRunRepository(store).create(submission())
    await work_queue.enqueue(item())
    corrupt = await store.get(collection="work_items", document_id="work-one")
    assert corrupt is not None
    corrupt["work_id"] = "work-two"
    await store.set(
        collection="work_items",
        document_id="work-one",
        document=corrupt,
    )

    assert await work_queue.cancel(tenant_id="tenant-one", run_id="run-one") == 0
    assert await store.get(collection="work_items", document_id="work-one") == corrupt
    assert len(client._tasks) == 1


@pytest.mark.asyncio
async def test_cancel_transaction_retry_rechecks_stored_identity() -> None:
    store = RetryingIdentityStore()
    client = FakeCloudTaskClient()
    work_queue = queue(store, client)
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    await FirestoreRunRepository(store).create(submission())
    await work_queue.enqueue(item())
    replacement = await store.get(collection="work_items", document_id="work-one")
    assert replacement is not None
    replacement["work_id"] = "work-two"
    store.retry_replacement = replacement

    assert await work_queue.cancel(tenant_id="tenant-one", run_id="run-one") == 0
    assert await store.get(collection="work_items", document_id="work-one") == (
        replacement
    )


@pytest.mark.asyncio
async def test_worker_acknowledges_a_legacy_delivery_without_claiming() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    replacement = (await runs.create(submission())).run
    executor = CompletedExecutor()
    worker = LocalWorker(
        repository=runs,
        queue=queue(store, FakeCloudTaskClient()),
        executor=executor,
        worker_id="worker-one",
        clock=lambda: NOW,
    )

    assert await worker.process(item())

    stored = await runs.get(tenant_id="tenant-one", run_id="run-one")
    assert stored is not None
    assert stored.generation_id == replacement.generation_id
    assert stored.state is RunState.QUEUED
    assert not executor.calls


@pytest.mark.asyncio
async def test_worker_acknowledges_stale_delivery_without_claiming_replacement() -> (
    None
):
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    old_run = (await runs.create(submission())).run
    assert await runs.delete_run(tenant_id="tenant-one", run_id="run-one")
    replacement = (await runs.create(submission())).run
    executor = CompletedExecutor()
    work_queue = queue(store, FakeCloudTaskClient())
    worker = LocalWorker(
        repository=runs,
        queue=work_queue,
        executor=executor,
        worker_id="worker-one",
        clock=lambda: NOW,
    )

    assert await worker.process(item(generation_id=old_run.generation_id))

    stored = await runs.get(tenant_id="tenant-one", run_id="run-one")
    assert stored is not None
    assert stored.generation_id == replacement.generation_id
    assert stored.state is RunState.QUEUED
    assert not executor.calls
