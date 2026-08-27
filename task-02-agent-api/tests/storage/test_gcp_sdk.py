from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from google.api_core.exceptions import AlreadyExists, InternalServerError, NotFound
from google.cloud.tasks_v2.types import Task

import agent_api.storage.gcp as gcp_storage
from agent_api.storage import CloudTask, CloudTaskAlreadyExistsError
from agent_api.storage.gcp import (
    GoogleCloudTaskClient,
    GoogleFirestoreDocumentStore,
)


class FakeSnapshot:
    def __init__(self, document: dict[str, object] | None) -> None:
        self.exists = document is not None
        self._document = document

    def to_dict(self) -> dict[str, object] | None:
        return self._document


class FakeDocumentReference:
    def __init__(
        self, store: FakeAsyncClient, collection: str, document_id: str
    ) -> None:
        self._store = store
        self.collection = collection
        self.document_id = document_id

    async def get(self) -> FakeSnapshot:
        return FakeSnapshot(
            self._store.documents.get(self.collection, {}).get(self.document_id)
        )


class FakeQuery:
    def __init__(
        self,
        store: FakeAsyncClient,
        collection: str,
        *,
        filters: list[tuple[str, object]] | None = None,
        order_by: tuple[str, ...] = (),
        start_after: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> None:
        self._store = store
        self.collection = collection
        self.filters = [] if filters is None else filters
        self.ordering = order_by
        self.cursor = None if start_after is None else dict(start_after)
        self.capped = limit

    def where(self, field_path: str, op_string: str, value: object) -> FakeQuery:
        assert op_string == "=="
        return FakeQuery(
            self._store,
            self.collection,
            filters=[*self.filters, (field_path, value)],
            order_by=self.ordering,
            start_after=self.cursor,
            limit=self.capped,
        )

    def order_by(self, field_path: str, direction: str = "ASCENDING") -> FakeQuery:
        assert direction == "ASCENDING"
        return FakeQuery(
            self._store,
            self.collection,
            filters=self.filters,
            order_by=(*self.ordering, field_path),
            start_after=self.cursor,
            limit=self.capped,
        )

    def start_after(self, cursor: Mapping[str, object]) -> FakeQuery:
        return FakeQuery(
            self._store,
            self.collection,
            filters=self.filters,
            order_by=self.ordering,
            start_after=cursor,
            limit=self.capped,
        )

    def limit(self, count: int) -> FakeQuery:
        return FakeQuery(
            self._store,
            self.collection,
            filters=self.filters,
            order_by=self.ordering,
            start_after=self.cursor,
            limit=count,
        )

    async def stream(self) -> AsyncIterator[FakeSnapshot]:
        for row in self._store.query_rows(self):
            yield FakeSnapshot(row)


class FakeCollectionReference(FakeQuery):
    def document(self, document_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(self._store, self.collection, document_id)


class FakeAsyncTransaction:
    def __init__(self, store: FakeAsyncClient) -> None:
        self._store = store
        self.writes: list[tuple[str, str, dict[str, object]] | tuple[str, str]] = []

    async def get(
        self, ref_or_query: FakeDocumentReference | FakeQuery
    ) -> AsyncIterator[FakeSnapshot]:
        if isinstance(ref_or_query, FakeDocumentReference):
            yield FakeSnapshot(
                self._store.documents.get(ref_or_query.collection, {}).get(
                    ref_or_query.document_id
                )
            )
            return
        for row in self._store.query_rows(ref_or_query):
            yield FakeSnapshot(row)

    def set(
        self, reference: FakeDocumentReference, document_data: dict[str, object]
    ) -> None:
        self._store.documents.setdefault(reference.collection, {})[
            reference.document_id
        ] = dict(document_data)
        self.writes.append(("set", reference.document_id, dict(document_data)))

    def delete(self, reference: FakeDocumentReference) -> None:
        self._store.documents.get(reference.collection, {}).pop(
            reference.document_id, None
        )
        self.writes.append(("delete", reference.document_id))


class FakeAsyncClient:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, dict[str, object]]] = {}
        self.last_transaction: FakeAsyncTransaction | None = None

    def collection(self, collection: str) -> FakeCollectionReference:
        return FakeCollectionReference(self, collection)

    def transaction(
        self, max_attempts: int = 5, read_only: bool = False
    ) -> FakeAsyncTransaction:
        assert max_attempts == 5
        assert read_only is False
        self.last_transaction = FakeAsyncTransaction(self)
        return self.last_transaction

    def query_rows(self, query: FakeQuery) -> list[dict[str, object]]:
        rows = list(self.documents.get(query.collection, {}).values())
        for key, expected in query.filters:
            rows = [row for row in rows if row.get(key) == expected]
        for field in reversed(query.ordering):
            rows.sort(key=lambda row: str(row[field]))
        if query.cursor is not None:
            rows = [
                row
                for row in rows
                if tuple(row[field] for field in query.ordering)
                > tuple(query.cursor[field] for field in query.ordering)
            ]
        if query.capped is not None:
            rows = rows[: query.capped]
        return [dict(row) for row in rows]


class FakeCloudTasksAsyncClient:
    def __init__(self) -> None:
        self.created_parent: str | None = None
        self.created_task: Task | None = None
        self.task: Task | None = None
        self.raise_create: Exception | None = None
        self.raise_get: Exception | None = None
        self.raise_delete: Exception | None = None
        self.raise_queue: Exception | None = None
        self.return_basic_response = False

    async def create_task(self, *, parent: str, task: Task) -> Task:
        if self.raise_create is not None:
            raise self.raise_create
        self.created_parent = parent
        self.created_task = task
        created = Task(name=task.name, schedule_time=task.schedule_time)
        if not self.return_basic_response:
            created = Task(task)
        self.task = created
        return created

    async def get_task(self, request: Mapping[str, object]) -> Task:
        if self.raise_get is not None:
            raise self.raise_get
        assert request["response_view"] == Task.View.FULL
        assert self.task is not None
        return self.task

    async def delete_task(self, *, name: str) -> None:
        if self.raise_delete is not None:
            raise self.raise_delete
        assert self.task is None or self.task.name == name

    async def get_queue(self, *, name: str) -> object:
        if self.raise_queue is not None:
            raise self.raise_queue
        return {"name": name}


@pytest.mark.asyncio
async def test_firestore_store_builds_filtered_ordered_query() -> None:
    client = FakeAsyncClient()
    client.documents["sessions"] = {
        "1": {
            "tenant_id": "t",
            "created_at": "2026-01-01T00:00:00+00:00",
            "session_id": "a",
        },
        "2": {
            "tenant_id": "t",
            "created_at": "2026-01-02T00:00:00+00:00",
            "session_id": "b",
        },
        "3": {
            "tenant_id": "u",
            "created_at": "2026-01-03T00:00:00+00:00",
            "session_id": "c",
        },
    }

    store = GoogleFirestoreDocumentStore(cast(gcp_storage._FirestoreClient, client))
    rows = await store.list(
        collection="sessions",
        filters={"tenant_id": "t"},
        order_by=("created_at", "session_id"),
        start_after={"created_at": "2026-01-01T00:00:00+00:00", "session_id": "a"},
        limit=1,
    )

    assert rows == (
        {
            "tenant_id": "t",
            "created_at": "2026-01-02T00:00:00+00:00",
            "session_id": "b",
        },
    )


@pytest.mark.asyncio
async def test_firestore_store_uses_async_transactional_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeAsyncClient()
    store = GoogleFirestoreDocumentStore(cast(gcp_storage._FirestoreClient, client))
    calls: list[FakeAsyncTransaction] = []

    def fake_async_transactional(
        function: Callable[[FakeAsyncTransaction], Awaitable[None]],
    ) -> Callable[[FakeAsyncTransaction], Awaitable[None]]:
        async def wrapped(transaction: FakeAsyncTransaction) -> None:
            calls.append(transaction)
            await function(transaction)

        return wrapped

    monkeypatch.setattr(
        "agent_api.storage.gcp.async_transactional", fake_async_transactional
    )

    async def write_run(tx: object) -> None:
        await cast(Callable[..., Awaitable[None]], tx.set)(  # type: ignore[attr-defined]
            collection="runs",
            document_id="run-1",
            document={"run_id": "run-1"},
        )

    await store.transaction(write_run)
    assert client.last_transaction is not None
    assert calls == [client.last_transaction]
    assert client.documents == {"runs": {"run-1": {"run_id": "run-1"}}}


@pytest.mark.asyncio
async def test_firestore_transaction_get_and_delete_translate_missing_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeAsyncClient()
    store = GoogleFirestoreDocumentStore(cast(gcp_storage._FirestoreClient, client))

    monkeypatch.setattr(
        "agent_api.storage.gcp.async_transactional",
        lambda function: function,
    )
    found = await store.transaction(
        lambda tx: tx.get(collection="runs", document_id="missing")
    )
    deleted = await store.transaction(
        lambda tx: tx.delete(collection="runs", document_id="missing")
    )

    assert found is None
    assert deleted is False


@pytest.mark.asyncio
async def test_cloud_tasks_create_requires_target_url() -> None:
    client = FakeCloudTasksAsyncClient()
    queue = GoogleCloudTaskClient(
        cast(gcp_storage._CloudTasksClient, client), "queues/main", None
    )

    with pytest.raises(ValueError, match="target URL"):
        await queue.create(
            CloudTask(
                name="queues/main/tasks/work-1",
                schedule_at=datetime(2026, 1, 1, tzinfo=UTC),
                body=b"{}",
                headers=(("X-Test", "1"),),
            )
        )


@pytest.mark.asyncio
async def test_cloud_tasks_create_and_get_round_trip() -> None:
    client = FakeCloudTasksAsyncClient()
    queue = GoogleCloudTaskClient(
        cast(gcp_storage._CloudTasksClient, client),
        "projects/p/locations/l/queues/main",
        "https://example.com/internal/tasks/run-delivery",
    )
    task = CloudTask(
        name="projects/p/locations/l/queues/main/tasks/work-1",
        schedule_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        body=b'{"ok":true}',
        headers=(("Content-Type", "application/json"), ("X-Test", "1")),
    )

    created = await queue.create(task)
    loaded = await queue.get(name=task.name)

    assert client.created_parent == "projects/p/locations/l/queues/main"
    assert client.created_task is not None
    assert created == task
    assert loaded == task


@pytest.mark.asyncio
async def test_cloud_tasks_create_preserves_requested_task_on_basic_response() -> None:
    client = FakeCloudTasksAsyncClient()
    client.return_basic_response = True
    queue = GoogleCloudTaskClient(
        cast(gcp_storage._CloudTasksClient, client),
        "projects/p/locations/l/queues/main",
        "https://example.com/internal/tasks/run-delivery",
    )
    task = CloudTask(
        name="projects/p/locations/l/queues/main/tasks/work-1",
        schedule_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        body=b'{"ok":true}',
        headers=(("Content-Type", "application/json"), ("X-Test", "1")),
    )

    assert await queue.create(task) == task


@pytest.mark.asyncio
async def test_cloud_tasks_translate_already_exists_and_not_found() -> None:
    client = FakeCloudTasksAsyncClient()
    client.raise_create = AlreadyExists("duplicate")  # type: ignore[no-untyped-call]
    client.raise_get = NotFound("missing")  # type: ignore[no-untyped-call]
    client.raise_delete = NotFound("missing")  # type: ignore[no-untyped-call]
    queue = GoogleCloudTaskClient(
        cast(gcp_storage._CloudTasksClient, client),
        "projects/p/locations/l/queues/main",
        "https://example.com/internal/tasks/run-delivery",
    )

    with pytest.raises(CloudTaskAlreadyExistsError):
        await queue.create(
            CloudTask(
                name="projects/p/locations/l/queues/main/tasks/work-1",
                schedule_at=datetime(2026, 1, 1, tzinfo=UTC),
                body=b"{}",
                headers=(),
            )
        )

    assert (
        await queue.get(name="projects/p/locations/l/queues/main/tasks/work-1") is None
    )
    assert (
        await queue.delete(name="projects/p/locations/l/queues/main/tasks/work-1")
        is False
    )


@pytest.mark.asyncio
async def test_cloud_tasks_ready_reports_queue_health() -> None:
    client = FakeCloudTasksAsyncClient()
    queue = GoogleCloudTaskClient(
        cast(gcp_storage._CloudTasksClient, client),
        "projects/p/locations/l/queues/main",
        "https://example.com/internal/tasks/run-delivery",
    )

    assert await queue.ready() is True

    client.raise_queue = NotFound("missing")  # type: ignore[no-untyped-call]
    assert await queue.ready() is False

    client.raise_queue = InternalServerError("boom")  # type: ignore[no-untyped-call]
    assert await queue.ready() is False
