"""Concrete Google Cloud adapters for the durable storage contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol, cast

from google.api_core.exceptions import AlreadyExists, GoogleAPICallError, NotFound
from google.cloud.firestore_v1.async_transaction import (
    AsyncTransaction,
    async_transactional,
)
from google.cloud.tasks_v2.types import HttpMethod, Task
from google.protobuf.timestamp_pb2 import Timestamp  # type: ignore[import-untyped]

from .cloud import (
    CloudTask,
    CloudTaskAlreadyExistsError,
    CloudTaskClient,
    DocumentStore,
    DocumentStoreTransaction,
)


class GoogleFirestoreDocumentStore(DocumentStore):
    def __init__(self, client: _FirestoreClient) -> None:
        self._client = client

    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None:
        snapshot = await self._client.collection(collection).document(document_id).get()
        return _snapshot_document(snapshot)

    async def list(
        self,
        *,
        collection: str,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        start_after: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        query = _build_query(
            self._client.collection(collection),
            filters=filters,
            order_by=order_by,
            start_after=start_after,
            limit=limit,
        )
        rows: list[dict[str, object]] = []
        async for snapshot in query.stream():
            document = _snapshot_document(snapshot)
            if document is not None:
                rows.append(document)
        return tuple(rows)

    async def transaction[T](
        self, operation: Callable[[DocumentStoreTransaction], Awaitable[T]]
    ) -> T:
        transaction = self._client.transaction()

        @async_transactional
        async def run(tx: AsyncTransaction) -> T:
            return await operation(
                _GoogleFirestoreTransaction(
                    self._client,
                    cast(_FirestoreTransaction, tx),
                )
            )

        return await run(cast(AsyncTransaction, transaction))


class _GoogleFirestoreTransaction(DocumentStoreTransaction):
    def __init__(
        self, client: _FirestoreClient, transaction: _FirestoreTransaction
    ) -> None:
        self._client = client
        self._transaction = transaction

    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None:
        document = self._client.collection(collection).document(document_id)
        async for snapshot in _stream_items(self._transaction.get(document)):
            return _snapshot_document(snapshot)
        return None

    async def set(
        self, *, collection: str, document_id: str, document: Mapping[str, object]
    ) -> None:
        reference = self._client.collection(collection).document(document_id)
        self._transaction.set(reference, dict(document))

    async def delete(self, *, collection: str, document_id: str) -> bool:
        reference = self._client.collection(collection).document(document_id)
        async for snapshot in _stream_items(self._transaction.get(reference)):
            if not snapshot.exists:
                return False
            self._transaction.delete(reference)
            return True
        return False

    async def list(
        self,
        *,
        collection: str,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        start_after: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        query = _build_query(
            self._client.collection(collection),
            filters=filters,
            order_by=order_by,
            start_after=start_after,
            limit=limit,
        )
        rows: list[dict[str, object]] = []
        async for snapshot in _stream_items(self._transaction.get(query)):
            document = _snapshot_document(snapshot)
            if document is not None:
                rows.append(document)
        return tuple(rows)


class GoogleCloudTaskClient(CloudTaskClient):
    def __init__(
        self,
        client: _CloudTasksClient,
        queue_name: str,
        target_url: str | None,
    ) -> None:
        self._client = client
        self._queue_name = queue_name
        self._target_url = target_url

    async def create(self, task: CloudTask) -> CloudTask:
        if type(self._target_url) is not str or not self._target_url:
            raise ValueError("cloud task target URL is required")
        try:
            await self._client.create_task(
                parent=self._queue_name,
                task=Task(
                    {
                        "name": task.name,
                        "schedule_time": _timestamp_message(task.schedule_at),
                        "http_request": {
                            "http_method": HttpMethod.POST,
                            "url": self._target_url,
                            "headers": dict(task.headers),
                            "body": task.body,
                        },
                    }
                )
            )
        except AlreadyExists as exc:
            raise CloudTaskAlreadyExistsError(task.name) from exc
        return task

    async def get(self, *, name: str) -> CloudTask | None:
        try:
            task = await self._client.get_task(
                request={"name": name, "response_view": Task.View.FULL}
            )
        except NotFound:
            return None
        return _decode_task(task)

    async def delete(self, *, name: str) -> bool:
        try:
            await self._client.delete_task(name=name)
        except NotFound:
            return False
        return True

    async def ready(self) -> bool:
        try:
            await self._client.get_queue(name=self._queue_name)
        except (GoogleAPICallError, NotFound):
            return False
        return True


def _build_query(
    query: _FirestoreQuery,
    *,
    filters: Mapping[str, object] | None,
    order_by: tuple[str, ...],
    start_after: Mapping[str, object] | None,
    limit: int | None,
) -> _FirestoreQuery:
    built = query
    if filters is not None:
        for key, value in filters.items():
            built = built.where(key, "==", value)
    for field in order_by:
        built = built.order_by(field)
    if start_after is not None:
        built = built.start_after(dict(start_after))
    if limit is not None:
        built = built.limit(limit)
    return built


def _snapshot_document(snapshot: object) -> dict[str, object] | None:
    checked = cast(_Snapshot, snapshot)
    if not checked.exists:
        return None
    document = checked.to_dict()
    if type(document) is not dict:
        raise TypeError("Firestore document payload must be an object")
    return document


def _timestamp_message(value: datetime) -> Timestamp:
    timestamp = Timestamp()
    timestamp.FromDatetime(value.astimezone(UTC))
    return timestamp


def _decode_task(task: Task) -> CloudTask:
    body = bytes(task.http_request.body)
    headers = tuple(sorted(dict(task.http_request.headers).items()))
    schedule_time = task.schedule_time
    to_datetime = getattr(schedule_time, "ToDatetime", None)
    if callable(to_datetime):
        schedule_at = to_datetime(tzinfo=UTC)
    else:
        schedule_at = cast(datetime, schedule_time).astimezone(UTC)
    return CloudTask(
        name=task.name,
        schedule_at=schedule_at,
        body=body,
        headers=headers,
    )


class _Snapshot(Protocol):
    @property
    def exists(self) -> bool: ...

    def to_dict(self) -> dict[str, object] | None: ...


async def _stream_items(
    items: AsyncIterator[_Snapshot] | Awaitable[AsyncIterator[_Snapshot]],
) -> AsyncIterator[_Snapshot]:
    if hasattr(items, "__await__"):
        stream = await cast(Awaitable[AsyncIterator[_Snapshot]], items)
    else:
        stream = items
    async for item in stream:
        yield item


class _FirestoreDocumentReference(Protocol):
    async def get(self) -> _Snapshot: ...


class _FirestoreQuery(Protocol):
    def where(
        self, field_path: str, op_string: str, value: object
    ) -> _FirestoreQuery: ...

    def order_by(
        self, field_path: str, direction: str = "ASCENDING"
    ) -> _FirestoreQuery: ...

    def start_after(
        self, document_fields_or_snapshot: Mapping[str, object]
    ) -> _FirestoreQuery: ...

    def limit(self, count: int) -> _FirestoreQuery: ...

    def stream(self) -> AsyncIterator[_Snapshot]: ...


class _FirestoreCollectionReference(_FirestoreQuery, Protocol):
    def document(self, document_id: str) -> _FirestoreDocumentReference: ...


class _FirestoreTransaction(Protocol):
    def get(
        self, ref_or_query: _FirestoreDocumentReference | _FirestoreQuery
    ) -> AsyncIterator[_Snapshot] | Awaitable[AsyncIterator[_Snapshot]]: ...

    def set(
        self, reference: _FirestoreDocumentReference, document_data: dict[str, object]
    ) -> None: ...

    def delete(self, reference: _FirestoreDocumentReference) -> None: ...


class _FirestoreClient(Protocol):
    def collection(self, collection: str) -> _FirestoreCollectionReference: ...

    def transaction(
        self, max_attempts: int = 5, read_only: bool = False
    ) -> _FirestoreTransaction: ...


class _CloudTasksClient(Protocol):
    async def create_task(self, *, parent: str, task: Task) -> Task: ...

    async def get_task(self, request: Mapping[str, object]) -> Task: ...

    async def delete_task(self, *, name: str) -> None: ...

    async def get_queue(self, *, name: str) -> object: ...


__all__ = [
    "GoogleCloudTaskClient",
    "GoogleFirestoreDocumentStore",
]
