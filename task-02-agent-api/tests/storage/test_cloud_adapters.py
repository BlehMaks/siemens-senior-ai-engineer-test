from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import AnyHttpUrl
from test_ports_contract import (
    NOW,
    RunRepositoryContract,
    WorkQueueContract,
    submission,
)

from agent_api.app import create_app
from agent_api.ports import ClaimRequest, RunRepository, RunState, WorkItem, WorkQueue
from agent_api.schemas import RunEvent, RunEventType
from agent_api.security import ExecutionPermit, RunAdmission, SSEPermit
from agent_api.storage import (
    CloudTask,
    CloudTaskAlreadyExistsError,
    CloudTasksWorkQueue,
    DocumentStoreTransaction,
    FirestoreEventRepository,
    FirestoreRunRepository,
    FirestoreSessionRepository,
    SessionRecord,
    SignedWorkItemCodec,
    SQLiteEventRepository,
    SQLiteRunRepository,
    SQLiteWorkQueue,
    StorageConflictError,
    TaskDeliveryAuthError,
)
from search_agent import (
    Citation,
    ExtractedEvidence,
    PublicEvent,
    QueryPlan,
    RunResult,
    RunStateGraph,
    RunUsage,
    ScopedAnswer,
    SearchHit,
    SearchQuery,
    ToolBudget,
)
from search_agent.contracts import EventType, OpaqueId, QueryText


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


class StubQuotaLimiter:
    async def admit_request(self, **_: object) -> None:
        return None

    async def admit_run(
        self,
        *,
        tenant_id: OpaqueId,
        key_id: OpaqueId,
        session_id: OpaqueId,
        idempotency_key: str,
        query: QueryText,
        run_id: OpaqueId,
        at: object,
    ) -> RunAdmission:
        del key_id, session_id, query, at
        return RunAdmission(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            run_id=run_id,
            created=True,
        )

    async def release_run(self, admission: RunAdmission) -> None:
        del admission

    async def acquire_execution(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        at: object,
        lease_seconds: int,
    ) -> ExecutionPermit:
        del at, lease_seconds
        return ExecutionPermit(
            tenant_id=tenant_id,
            run_id=run_id,
            permit_id="permit-fixed",
        )

    async def release_execution(self, permit: ExecutionPermit) -> None:
        del permit

    async def renew_execution(
        self, permit: ExecutionPermit, *, at: object, lease_seconds: int
    ) -> bool:
        del permit, at, lease_seconds
        return True

    async def acquire_sse(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId, at: object
    ) -> SSEPermit:
        del at
        return SSEPermit(
            tenant_id=tenant_id,
            key_id=key_id,
            permit_id="sse-fixed",
        )

    async def release_sse(self, permit: SSEPermit) -> None:
        del permit

    async def renew_sse(self, permit: SSEPermit, *, at: object) -> bool:
        del permit, at
        return True


class BlockedQuotaLimiter(StubQuotaLimiter):
    async def acquire_execution(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        at: object,
        lease_seconds: int,
    ) -> None:
        del tenant_id, run_id, at, lease_seconds
        return None


class CompletedExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def run(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        request: QueryText,
    ) -> RunResult:
        self.calls.append((tenant_id, session_id, run_id, str(request)))
        snapshot = RunStateGraph.create(tenant_id, session_id, run_id, str(request))
        created = PublicEvent(
            tenant_id=snapshot.tenant_id,
            session_id=snapshot.session_id,
            run_id=snapshot.run_id,
            event_type=EventType.RUN_CREATED,
            message="Created bounded research run",
        )
        plan = QueryPlan(
            tool_budget=ToolBudget(max_search_queries=1, max_fetches=1),
            searches=(SearchQuery(text="Siemens official report", max_results=1),),
        )
        snapshot, planned = RunStateGraph.accept_plan(snapshot, plan)
        snapshot, started = RunStateGraph.start_search(snapshot)
        report_url = AnyHttpUrl("https://www.siemens.com/reports/sustainability-2025")
        hit = SearchHit(
            title="Siemens report",
            url=report_url,
            snippet="Official public report",
            rank=1,
        )
        evidence = ExtractedEvidence(
            evidence_id="ev-report",
            source_url=report_url,
            source_title="Siemens report",
            summary="Siemens published its 2025 sustainability report.",
            quotes=("Siemens published its 2025 sustainability report.",),
        )
        snapshot, recorded = RunStateGraph.record_evidence(
            snapshot, hits=(hit,), evidence=(evidence,)
        )
        answer = ScopedAnswer(
            answer_text="Siemens published its 2025 sustainability report.",
            citations=(
                Citation(
                    claim="Siemens published its 2025 sustainability report.",
                    evidence_id="ev-report",
                    source_url=report_url,
                ),
            ),
        )
        snapshot, drafted = RunStateGraph.draft_answer(snapshot, answer)
        snapshot, completed = RunStateGraph.complete(snapshot)
        return RunResult(
            snapshot=snapshot,
            events=(created, planned, started, recorded, drafted, completed),
            usage=RunUsage(
                elapsed_seconds=0.5,
                iterations=4,
                search_queries=1,
                pages=1,
                failed_pages=0,
                raw_bytes_reserved=128,
                decoded_bytes=64,
                model_calls=1,
                model_attempts=1,
                tokens=128,
            ),
        )


class FakeDocumentStore(DocumentStoreTransaction):
    def __init__(self) -> None:
        self._collections: dict[str, dict[str, dict[str, object]]] = {}
        self._lock = asyncio.Lock()

    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None:
        row = self._collections.get(collection, {}).get(document_id)
        return None if row is None else copy.deepcopy(row)

    async def set(
        self, *, collection: str, document_id: str, document: Mapping[str, object]
    ) -> None:
        self._collections.setdefault(collection, {})[document_id] = copy.deepcopy(
            dict(document)
        )

    async def delete(self, *, collection: str, document_id: str) -> bool:
        rows = self._collections.get(collection, {})
        existed = document_id in rows
        rows.pop(document_id, None)
        return existed

    async def list(
        self,
        *,
        collection: str,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        start_after: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        selected = []
        for row in self._collections.get(collection, {}).values():
            if filters is not None and any(
                row.get(key) != value for key, value in filters.items()
            ):
                continue
            selected.append(copy.deepcopy(row))
        if order_by:
            selected.sort(key=lambda row: tuple(row[field] for field in order_by))
        if start_after is not None:
            cursor = tuple(start_after[field] for field in order_by)
            selected = [
                row
                for row in selected
                if tuple(row[field] for field in order_by) > cursor
            ]
        if limit is not None:
            selected = selected[:limit]
        return tuple(selected)

    async def transaction[T](
        self, operation: Callable[[DocumentStoreTransaction], Awaitable[T]]
    ) -> T:
        async with self._lock:
            return await operation(ReadBeforeWriteTransaction(self))


class ReadBeforeWriteTransaction(DocumentStoreTransaction):
    def __init__(self, store: FakeDocumentStore) -> None:
        self._store = store
        self._wrote = False

    def _require_read_phase(self) -> None:
        if self._wrote:
            raise RuntimeError("Firestore transactions forbid read-after-write")

    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None:
        self._require_read_phase()
        return await self._store.get(collection=collection, document_id=document_id)

    async def list(
        self,
        *,
        collection: str,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        start_after: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        self._require_read_phase()
        return await self._store.list(
            collection=collection,
            filters=filters,
            order_by=order_by,
            start_after=start_after,
            limit=limit,
        )

    async def set(
        self, *, collection: str, document_id: str, document: Mapping[str, object]
    ) -> None:
        self._wrote = True
        await self._store.set(
            collection=collection,
            document_id=document_id,
            document=document,
        )

    async def delete(self, *, collection: str, document_id: str) -> bool:
        self._wrote = True
        return await self._store.delete(
            collection=collection,
            document_id=document_id,
        )


class FakeCloudTaskClient:
    def __init__(self) -> None:
        self._tasks: dict[str, CloudTask] = {}

    async def create(self, task: CloudTask) -> CloudTask:
        if task.name in self._tasks:
            raise CloudTaskAlreadyExistsError("task already exists")
        self._tasks[task.name] = task
        return task

    async def get(self, *, name: str) -> CloudTask | None:
        return self._tasks.get(name)

    async def delete(self, *, name: str) -> bool:
        return self._tasks.pop(name, None) is not None


async def _seed_session(
    store: FakeDocumentStore, *, tenant_id: str, session_id: str
) -> None:
    await store.transaction(
        lambda tx: tx.set(
            collection="sessions",
            document_id=f"{tenant_id}|{session_id}",
            document={
                "document_id": f"{tenant_id}|{session_id}",
                "tenant_id": tenant_id,
                "session_id": session_id,
                "created_at": NOW.isoformat(timespec="microseconds"),
                "updated_at": NOW.isoformat(timespec="microseconds"),
            },
        )
    )


class TestFirestoreRunRepository(RunRepositoryContract):
    @pytest_asyncio.fixture
    async def repository(self, tmp_path: Path) -> AsyncIterator[RunRepository]:
        del tmp_path
        store = FakeDocumentStore()
        for tenant_id, session_id in (
            ("tenant-one", "session-one"),
            ("tenant-one", "session-two"),
            ("tenant-two", "session-one"),
        ):
            await _seed_session(store, tenant_id=tenant_id, session_id=session_id)
        yield FirestoreRunRepository(store)


class TestCloudTasksWorkQueue(WorkQueueContract):
    @pytest_asyncio.fixture
    async def queue(self, tmp_path: Path) -> AsyncIterator[WorkQueue]:
        del tmp_path
        store = FakeDocumentStore()
        for tenant_id in ("tenant-one", "tenant-two"):
            await _seed_session(store, tenant_id=tenant_id, session_id="session-one")
        runs = FirestoreRunRepository(store)
        await runs.create(submission())
        await runs.create(
            submission(
                tenant_id="tenant-two",
                run_id="run-one",
                idempotency_key="request-key-two",
            )
        )
        yield CloudTasksWorkQueue(
            store=store,
            task_client=FakeCloudTaskClient(),
            queue_name="projects/test/locations/eu/queues/dispatch",
            codec=SignedWorkItemCodec(b"s" * 32),
        )


@pytest.mark.asyncio
async def test_firestore_sessions_are_pageable_run_parents_and_cascade_delete() -> None:
    store = FakeDocumentStore()
    runs = FirestoreRunRepository(store)
    sessions = FirestoreSessionRepository(store, runs)
    first = SessionRecord(
        tenant_id="tenant-one",
        session_id="session-one",
        created_at=NOW,
        updated_at=NOW,
    )
    second = SessionRecord(
        tenant_id="tenant-one",
        session_id="session-two",
        created_at=NOW + timedelta(seconds=1),
        updated_at=NOW + timedelta(seconds=1),
    )

    assert await sessions.put(first) is True
    assert await sessions.put(first) is False
    assert await sessions.put(second) is True
    assert await sessions.list(tenant_id="tenant-one", limit=1) == (first,)
    assert await sessions.list(
        tenant_id="tenant-one",
        limit=1,
        after=(first.created_at, first.session_id),
    ) == (second,)

    created = await runs.create(submission())
    assert created.created is True
    assert await sessions.delete(tenant_id="tenant-one", session_id="session-one")
    assert await runs.get(tenant_id="tenant-one", run_id="run-one") is None
    assert await sessions.get(tenant_id="tenant-one", session_id="session-one") is None


@pytest.mark.asyncio
async def test_firestore_event_ordering_and_finality_match_local_contract() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    events = FirestoreEventRepository(store)
    await runs.create(submission())

    for sequence in (2, 3, 4):
        assert await events.append(
            tenant_id="tenant-one",
            event=RunEvent(
                sequence=sequence,
                run_id="run-one",
                event_type=RunEventType.STATUS,
                state=RunState.QUEUED,
                occurred_at=NOW + timedelta(seconds=sequence),
                message="Run is queued.",
            ),
        )

    cancelled = await runs.request_cancellation(
        tenant_id="tenant-one",
        run_id="run-one",
        at=NOW + timedelta(seconds=5),
    )

    assert cancelled.changed is True
    resumed = await events.list(
        tenant_id="tenant-one",
        run_id="run-one",
        after_sequence=4,
    )
    assert len(resumed) == 1
    assert resumed[0].sequence == 5
    assert resumed[0].event_type is RunEventType.CANCELLED
    with pytest.raises(StorageConflictError, match="terminal event must remain final"):
        await events.append(
            tenant_id="tenant-one",
            event=RunEvent(
                sequence=6,
                run_id="run-one",
                event_type=RunEventType.STATUS,
                state=RunState.RUNNING,
                occurred_at=NOW + timedelta(seconds=6),
                message="Run execution is in progress.",
            ),
        )


@pytest.mark.asyncio
async def test_cloud_task_payloads_are_signed_and_tamper_evident() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    await runs.create(submission())
    client = FakeCloudTaskClient()
    queue_name = "projects/test/locations/eu/queues/dispatch"
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=client,
        queue_name=queue_name,
        codec=SignedWorkItemCodec(b"s" * 32),
    )
    item = WorkItem(
        work_id="work-one",
        tenant_id="tenant-one",
        run_id="run-one",
        enqueued_at=NOW,
        not_before=NOW,
    )

    created = await queue.enqueue(item)
    task = next(iter(client._tasks.values()))
    delivery_headers = dict(task.headers) | {
        "X-CloudTasks-TaskName": task.name,
        "X-CloudTasks-QueueName": queue_name,
    }

    assert created.created is True
    assert task is not None
    assert (
        queue.decode_delivery(
            body=task.body,
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=delivery_headers["X-CloudTasks-TaskName"],
            queue_name=delivery_headers["X-CloudTasks-QueueName"],
        )
        == item
    )
    assert (
        queue.decode_delivery(
            body=task.body,
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=task.name.rsplit("/", 1)[-1],
            queue_name=queue_name.rsplit("/", 1)[-1],
        )
        == item
    )
    with pytest.raises(TaskDeliveryAuthError, match="signature is invalid"):
        queue.decode_delivery(
            body=task.body + b" ",
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=delivery_headers["X-CloudTasks-TaskName"],
            queue_name=delivery_headers["X-CloudTasks-QueueName"],
        )
    with pytest.raises(TaskDeliveryAuthError, match="headers are incomplete"):
        queue.decode_delivery(
            body=task.body,
            signature=None,
            task_name=delivery_headers["X-CloudTasks-TaskName"],
            queue_name=delivery_headers["X-CloudTasks-QueueName"],
        )
    with pytest.raises(TaskDeliveryAuthError, match="queue is invalid"):
        queue.decode_delivery(
            body=task.body,
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=delivery_headers["X-CloudTasks-TaskName"],
            queue_name="projects/test/locations/eu/queues/other",
        )
    with pytest.raises(TaskDeliveryAuthError, match="signature is invalid"):
        queue.decode_delivery(
            body=task.body,
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=f"{queue_name}/tasks/other",
            queue_name=delivery_headers["X-CloudTasks-QueueName"],
        )


@pytest.mark.asyncio
async def test_signed_cloud_delivery_executes_once_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    repository = FirestoreRunRepository(store)
    events = FirestoreEventRepository(store)
    client = FakeCloudTaskClient()
    queue_name = "projects/test/locations/eu/queues/dispatch"
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=client,
        queue_name=queue_name,
        codec=SignedWorkItemCodec(b"s" * 32),
    )
    executor = CompletedExecutor()
    app = create_app(
        database_path=tmp_path / "cloud-delivery.sqlite3",
        pepper_provider=FixedPepper(),
        quota_limiter=StubQuotaLimiter(),
        run_executor=executor,
        run_repository=repository,
        event_repository=events,
        work_queue=queue,
        production_environment=True,
        run_state_backend="firestore",
        queue_backend="cloud_tasks",
        task_delivery_enabled=True,
    )
    created = await repository.create(submission())
    item = WorkItem(
        work_id=f"work-{created.run.run_id}",
        tenant_id=created.run.tenant_id,
        run_id=created.run.run_id,
        enqueued_at=created.run.created_at,
        not_before=created.run.created_at,
    )
    await queue.enqueue(item)
    task = next(iter(client._tasks.values()))
    headers = {name: value for name, value in task.headers} | {
        "X-CloudTasks-TaskName": task.name.rsplit("/", 1)[-1],
        "X-CloudTasks-QueueName": queue_name.rsplit("/", 1)[-1],
    }

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as web,
    ):
        first = await web.post(
            "/internal/tasks/run-delivery",
            content=task.body,
            headers=headers,
        )
        duplicate = await web.post(
            "/internal/tasks/run-delivery", content=task.body, headers=headers
        )
        tampered = await web.post(
            "/internal/tasks/run-delivery",
            content=task.body + b" ",
            headers=headers,
        )

    assert first.status_code == 204
    assert duplicate.status_code == 204
    assert tampered.status_code == 401
    assert tampered.json()["error"]["code"] == "unauthenticated"
    assert executor.calls == [
        (
            "tenant-one",
            "session-one",
            created.run.run_id,
            "find the documented answer",
        )
    ]
    stored = await repository.get(tenant_id="tenant-one", run_id=created.run.run_id)
    assert stored is not None
    assert stored.state is RunState.COMPLETED
    assert stored.delivery_attempts == 1
    assert tuple(
        event.state
        for event in await events.list(
            tenant_id="tenant-one", run_id=created.run.run_id
        )
    ) == (
        RunState.QUEUED,
        RunState.RUNNING,
        RunState.COMPLETED,
    )


@pytest.mark.parametrize("contention", ["quota", "lease"])
@pytest.mark.asyncio
async def test_temporary_cloud_delivery_contention_returns_retryable_status(
    tmp_path: Path,
    contention: str,
) -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    await runs.create(submission())
    client = FakeCloudTaskClient()
    queue_name = "projects/test/locations/eu/queues/dispatch"
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=client,
        queue_name=queue_name,
        codec=SignedWorkItemCodec(b"s" * 32),
    )
    await queue.enqueue(
        WorkItem(
            work_id="work-run-one",
            tenant_id="tenant-one",
            run_id="run-one",
            enqueued_at=NOW,
            not_before=NOW,
        )
    )
    if contention == "lease":
        await runs.claim(
            ClaimRequest(
                tenant_id="tenant-one",
                run_id="run-one",
                worker_id="worker-crashed",
                lease_id="lease-crashed",
                now=NOW,
                lease_seconds=30,
            )
        )
    task = next(iter(client._tasks.values()))
    app = create_app(
        database_path=tmp_path / f"{contention}-delivery.sqlite3",
        pepper_provider=FixedPepper(),
        clock=lambda: NOW + timedelta(seconds=1),
        quota_limiter=(
            BlockedQuotaLimiter() if contention == "quota" else StubQuotaLimiter()
        ),
        run_executor=CompletedExecutor(),
        run_repository=runs,
        event_repository=FirestoreEventRepository(store),
        work_queue=queue,
        production_environment=True,
        run_state_backend="firestore",
        queue_backend="cloud_tasks",
        task_delivery_enabled=True,
    )
    headers = dict(task.headers) | {
        "X-CloudTasks-TaskName": task.name.rsplit("/", 1)[-1],
        "X-CloudTasks-QueueName": queue_name.rsplit("/", 1)[-1],
    }

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as web,
    ):
        response = await web.post(
            "/internal/tasks/run-delivery",
            content=task.body,
            headers=headers,
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"


@pytest.mark.asyncio
async def test_production_application_creates_cloud_run_parent_sessions(
    tmp_path: Path,
) -> None:
    store = FakeDocumentStore()
    runs = FirestoreRunRepository(store)
    app = create_app(
        database_path=tmp_path / "cloud-session.sqlite3",
        pepper_provider=FixedPepper(),
        session_id_factory=lambda: "session-cloud",
        run_repository=runs,
        event_repository=FirestoreEventRepository(store),
        work_queue=CloudTasksWorkQueue(
            store=store,
            task_client=FakeCloudTaskClient(),
            queue_name="projects/test/locations/eu/queues/dispatch",
            codec=SignedWorkItemCodec(b"s" * 32),
        ),
        production_environment=True,
        run_state_backend="firestore",
        queue_backend="cloud_tasks",
    )

    async with app.router.lifespan_context(app):
        session = await app.state.session_service.create(
            tenant_id="tenant-one", label=None
        )
        created = await runs.create(
            submission(session_id=session.session_id, run_id="run-cloud")
        )

    assert created.created is True


def test_production_rejects_sqlite_adapters_with_cloud_backend_labels(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sqlite-disguised-as-cloud.sqlite3"
    path.touch()

    with pytest.raises(ValueError, match=r"Firestore|Cloud Tasks"):
        create_app(
            database_path=path,
            pepper_provider=FixedPepper(),
            run_repository=SQLiteRunRepository(path),
            event_repository=SQLiteEventRepository(path),
            work_queue=SQLiteWorkQueue(path),
            production_environment=True,
            run_state_backend="firestore",
            queue_backend="cloud_tasks",
        )


@pytest.mark.parametrize(
    ("event_repository", "run_executor", "task_delivery_enabled", "message"),
    [
        (None, None, False, "injected event repository"),
        ("injected", CompletedExecutor(), False, "signed task delivery"),
    ],
)
def test_production_runtime_rejects_partial_cloud_wiring(
    tmp_path: Path,
    event_repository: object,
    run_executor: CompletedExecutor | None,
    task_delivery_enabled: bool,
    message: str,
) -> None:
    store = FakeDocumentStore()
    runs = FirestoreRunRepository(store)
    events = FirestoreEventRepository(store)
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=FakeCloudTaskClient(),
        queue_name="projects/test/locations/eu/queues/dispatch",
        codec=SignedWorkItemCodec(b"s" * 32),
    )

    with pytest.raises(ValueError, match=message):
        create_app(
            database_path=tmp_path / "partial-cloud.sqlite3",
            pepper_provider=FixedPepper(),
            quota_limiter=StubQuotaLimiter(),
            run_executor=run_executor,
            run_repository=runs,
            event_repository=None if event_repository is None else events,
            work_queue=queue,
            production_environment=True,
            run_state_backend="firestore",
            queue_backend="cloud_tasks",
            task_delivery_enabled=task_delivery_enabled,
        )
