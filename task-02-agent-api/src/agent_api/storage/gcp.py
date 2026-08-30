"""Concrete Google Cloud adapters for the durable storage contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol, cast

from google.api_core.exceptions import AlreadyExists, NotFound
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
        document_id_prefix: str | None = None,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        start_after: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        query = _build_query(
            self._client.collection(collection),
            document_id_prefix=document_id_prefix,
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
        self._known_documents: set[tuple[str, str]] = set()
        self._missing_documents: set[tuple[str, str]] = set()

    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None:
        key = (collection, document_id)
        document = self._client.collection(collection).document(document_id)
        snapshot = await document.get(transaction=self._transaction)
        decoded = _snapshot_document(snapshot)
        if decoded is None:
            self._missing_documents.add(key)
        else:
            self._known_documents.add(key)
        return decoded

    async def set(
        self, *, collection: str, document_id: str, document: Mapping[str, object]
    ) -> None:
        reference = self._client.collection(collection).document(document_id)
        self._transaction.set(reference, dict(document))
        self._known_documents.add((collection, document_id))
        self._missing_documents.discard((collection, document_id))

    async def delete(self, *, collection: str, document_id: str) -> bool:
        reference = self._client.collection(collection).document(document_id)
        key = (collection, document_id)
        if key in self._known_documents:
            self._transaction.delete(reference)
            self._known_documents.discard(key)
            self._missing_documents.add(key)
            return True
        if key in self._missing_documents:
            return False
        snapshot = await reference.get(transaction=self._transaction)
        if not snapshot.exists:
            self._missing_documents.add(key)
            return False
        self._transaction.delete(reference)
        self._missing_documents.add(key)
        return True

    async def delete_known(self, *, collection: str, document_id: str) -> None:
        reference = self._client.collection(collection).document(document_id)
        self._transaction.delete(reference)

    async def list(
        self,
        *,
        collection: str,
        document_id_prefix: str | None = None,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        start_after: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        query = _build_query(
            self._client.collection(collection),
            document_id_prefix=document_id_prefix,
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
                document_id = document.get("document_id")
                if type(document_id) is str:
                    self._known_documents.add((collection, document_id))
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
                ),
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
        await self._client.get_queue(name=self._queue_name)
        return True


def _build_query(
    query: _FirestoreCollectionReference,
    *,
    document_id_prefix: str | None,
    filters: Mapping[str, object] | None,
    order_by: tuple[str, ...],
    start_after: Mapping[str, object] | None,
    limit: int | None,
) -> _FirestoreQuery:
    built: _FirestoreQuery = query
    if document_id_prefix is not None:
        lower = query.document(document_id_prefix)
        upper = query.document(_prefix_upper_bound(document_id_prefix))
        built = built.where("__name__", ">=", lower)
        built = built.where("__name__", "<", upper)
    if filters is not None:
        for key, value in filters.items():
            built = built.where(key, "==", value)
    for field in order_by:
        if field.startswith("-"):
            built = built.order_by(field[1:], direction="DESCENDING")
        else:
            built = built.order_by(field)
    if start_after is not None:
        built = built.start_after(dict(start_after))
    if limit is not None:
        built = built.limit(limit)
    return built


def _prefix_upper_bound(prefix: str) -> str:
    for index in range(len(prefix) - 1, -1, -1):
        codepoint = ord(prefix[index])
        if codepoint < 0x10FFFF:
            return prefix[:index] + chr(codepoint + 1)
    raise ValueError("document ID prefix has no finite upper bound")


def _snapshot_document(snapshot: object) -> dict[str, object] | None:
    checked = cast(_Snapshot, snapshot)
    if not checked.exists:
        return None
    document = checked.to_dict()
    if type(document) is not dict:
        raise TypeError("Firestore document payload must be an object")
    document["document_id"] = checked.id
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
    def id(self) -> str: ...

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
    async def get(
        self, *, transaction: _FirestoreTransaction | None = None
    ) -> _Snapshot: ...


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
