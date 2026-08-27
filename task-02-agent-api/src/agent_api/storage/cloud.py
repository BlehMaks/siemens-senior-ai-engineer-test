"""Cloud-ready document and task adapters for the durable run contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import TypeAdapter

from search_agent.contracts import OpaqueId, ScopedAnswer
from search_agent.memory import RunReflection

from ..ports import (
    TERMINAL_RUN_STATES,
    CancellationResult,
    ClaimDisposition,
    ClaimRequest,
    ClaimResult,
    CreateRunResult,
    EnqueueResult,
    ExecutionLease,
    IdempotencyConflictError,
    LeaseDisposition,
    LeaseRenewal,
    LeaseResult,
    QueueConflictError,
    RunFailureCode,
    RunParentNotFoundError,
    RunRecord,
    RunState,
    RunSubmission,
    StateUpdate,
    StateUpdateResult,
    WorkItem,
    WriteDisposition,
)
from ..schemas import RunEvent, RunEventType, RunFailure, public_run_failure
from .repositories import (
    StorageConflictError,
    StorageError,
    _changed_run,
    _lease_expiry,
    _parse_timestamp,
)

_OPAQUE_ID = TypeAdapter(OpaqueId)
_RUN_EVENT_TYPES = {
    RunState.COMPLETED: RunEventType.COMPLETED,
    RunState.FAILED: RunEventType.FAILED,
    RunState.CANCELLED: RunEventType.CANCELLED,
    RunState.EXPIRED: RunEventType.EXPIRED,
}
_RUN_EVENT_MESSAGES = {
    RunState.QUEUED: "Run accepted and queued.",
    RunState.RUNNING: "Run execution is in progress.",
    RunState.WAITING_FOR_TOOL: "Run is waiting for a bounded tool operation.",
    RunState.COMPLETED: "Run completed.",
    RunState.CANCELLED: "Run cancelled.",
}
_RUNS = "runs"
_SESSIONS = "sessions"
_IDEMPOTENCY = "idempotency_records"
_EVENTS = "run_events"
_REFLECTIONS = "run_reflections"
_WORK_ITEMS = "work_items"
_SIGNATURE_HEADER = "X-Agent-Api-Task-Signature"
_TASK_NAME_HEADER = "X-CloudTasks-TaskName"
_QUEUE_NAME_HEADER = "X-CloudTasks-QueueName"
_PAYLOAD_TYPE = "application/json"


def _scope_id(value: OpaqueId) -> OpaqueId:
    if type(value) is not str:
        raise ValueError("scope id must be a string")
    return _OPAQUE_ID.validate_python(value, strict=True)


def _timestamp(value: datetime) -> str:
    if type(value) is not datetime:
        raise ValueError("timestamp must be an exact datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.isoformat(timespec="microseconds")


def _checked_run(value: RunRecord) -> RunRecord:
    if type(value) is not RunRecord:
        raise ValueError("storage value has the wrong concrete type")
    return RunRecord.model_validate(value.model_dump(mode="python"))


def _checked_submission(value: RunSubmission) -> RunSubmission:
    if type(value) is not RunSubmission:
        raise ValueError("storage value has the wrong concrete type")
    return RunSubmission.model_validate(value.model_dump(mode="python"))


def _checked_claim(value: ClaimRequest) -> ClaimRequest:
    if type(value) is not ClaimRequest:
        raise ValueError("storage value has the wrong concrete type")
    return ClaimRequest.model_validate(value.model_dump(mode="python"))


def _checked_renewal(value: LeaseRenewal) -> LeaseRenewal:
    if type(value) is not LeaseRenewal:
        raise ValueError("storage value has the wrong concrete type")
    return LeaseRenewal.model_validate(value.model_dump(mode="python"))


def _checked_update(value: StateUpdate) -> StateUpdate:
    if type(value) is not StateUpdate:
        raise ValueError("storage value has the wrong concrete type")
    return StateUpdate.model_validate(value.model_dump(mode="python"))


def _checked_item(value: WorkItem) -> WorkItem:
    if type(value) is not WorkItem:
        raise ValueError("storage value has the wrong concrete type")
    return WorkItem.model_validate(value.model_dump(mode="python"))


class DocumentStoreTransaction(Protocol):
    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None: ...

    async def set(
        self, *, collection: str, document_id: str, document: Mapping[str, object]
    ) -> None: ...

    async def delete(self, *, collection: str, document_id: str) -> bool: ...

    async def list(
        self,
        *,
        collection: str,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]: ...


class DocumentStore(Protocol):
    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None: ...

    async def list(
        self,
        *,
        collection: str,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]: ...

    async def transaction[T](
        self, operation: Callable[[DocumentStoreTransaction], Awaitable[T]]
    ) -> T: ...


@dataclass(frozen=True, slots=True)
class CloudTask:
    name: str
    schedule_at: datetime
    body: bytes
    headers: tuple[tuple[str, str], ...]


class CloudTaskClient(Protocol):
    async def create(self, task: CloudTask) -> CloudTask: ...

    async def get(self, *, name: str) -> CloudTask | None: ...

    async def delete(self, *, name: str) -> bool: ...


class CloudTaskAlreadyExistsError(ValueError):
    """A deterministic task name was already claimed."""


class TaskDeliveryAuthError(RuntimeError):
    """Signed delivery failed a permanent integrity check."""


class SignedWorkItemCodec:
    def __init__(self, secret: bytes) -> None:
        if type(secret) is not bytes or len(secret) < 32:
            raise ValueError("task signing secret must contain at least 32 bytes")
        self._secret = secret

    def encode(
        self, item: WorkItem, *, task_name: str, queue_name: str
    ) -> tuple[bytes, tuple[tuple[str, str], ...]]:
        checked = _checked_item(item)
        _require_delivery_identity(task_name=task_name, queue_name=queue_name)
        body = checked.model_dump_json().encode("utf-8")
        signature = hmac.new(
            self._secret,
            _delivery_signature_input(
                body=body, task_name=task_name, queue_name=queue_name
            ),
            "sha256",
        ).hexdigest()
        return body, (
            ("Content-Type", _PAYLOAD_TYPE),
            (_SIGNATURE_HEADER, f"sha256={signature}"),
        )

    def decode(
        self,
        *,
        body: bytes,
        signature: str | None,
        task_name: str | None,
        queue_name: str | None,
    ) -> WorkItem:
        if type(signature) is not str or not signature.startswith("sha256="):
            raise TaskDeliveryAuthError("task delivery headers are incomplete")
        try:
            _require_delivery_identity(task_name=task_name, queue_name=queue_name)
        except ValueError as exc:
            raise TaskDeliveryAuthError("task delivery headers are incomplete") from exc
        assert task_name is not None and queue_name is not None
        expected = "sha256=" + hmac.new(
            self._secret,
            _delivery_signature_input(
                body=body, task_name=task_name, queue_name=queue_name
            ),
            "sha256",
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise TaskDeliveryAuthError("task delivery signature is invalid")
        try:
            return WorkItem.model_validate_json(body)
        except ValueError as exc:
            raise TaskDeliveryAuthError("task delivery body is invalid") from exc


class FirestoreRunRepository:
    """Document-store run state that preserves the local contract semantics."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    async def create(self, submission: RunSubmission) -> CreateRunResult:
        checked = _checked_submission(submission)
        request_hash = hashlib.sha256(
            json.dumps(
                [checked.session_id, checked.query],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

        async def operation(
            tx: DocumentStoreTransaction,
        ) -> CreateRunResult:
            idempotency_id = _document_id(checked.tenant_id, checked.idempotency_key)
            existing = await tx.get(collection=_IDEMPOTENCY, document_id=idempotency_id)
            if existing is not None:
                if existing.get("request_hash") != request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key already identifies another request"
                    )
                run = await self._tx_get_run(
                    tx, tenant_id=checked.tenant_id, run_id=_require_text(existing, "run_id")
                )
                if run is None:
                    raise StorageError("idempotency record has no run")
                return CreateRunResult(run=run, created=False)
            if await self._tx_get_run(
                tx, tenant_id=checked.tenant_id, run_id=checked.run_id
            ) is not None:
                raise IdempotencyConflictError("run id already exists")
            if (
                await tx.get(
                    collection=_SESSIONS,
                    document_id=_document_id(checked.tenant_id, checked.session_id),
                )
                is None
            ):
                raise RunParentNotFoundError("referenced parent object does not exist")
            run = RunRecord(
                **checked.model_dump(mode="python", exclude={"created_at"}),
                state=RunState.QUEUED,
                version=0,
                delivery_attempts=0,
                created_at=checked.created_at,
                updated_at=checked.created_at,
                answer=None,
                failure_code=None,
            )
            await tx.set(
                collection=_RUNS,
                document_id=_document_id(run.tenant_id, run.run_id),
                document=_run_document(run),
            )
            await tx.set(
                collection=_IDEMPOTENCY,
                document_id=idempotency_id,
                document={
                    "document_id": idempotency_id,
                    "tenant_id": checked.tenant_id,
                    "idempotency_key": checked.idempotency_key,
                    "request_hash": request_hash,
                    "run_id": checked.run_id,
                    "created_at": _timestamp(checked.created_at),
                },
            )
            await self._append_event(tx, run)
            return CreateRunResult(run=run, created=True)

        return await self._store.transaction(operation)

    async def get(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> RunRecord | None:
        return _decode_run_document(
            await self._store.get(
                collection=_RUNS,
                document_id=_document_id(_scope_id(tenant_id), _scope_id(run_id)),
            )
        )

    async def list_session(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, limit: int = 100
    ) -> tuple[RunRecord, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        rows = await self._store.list(
            collection=_RUNS,
            filters={
                "tenant_id": _scope_id(tenant_id),
                "session_id": _scope_id(session_id),
            },
            order_by=("created_at", "run_id"),
            limit=limit,
        )
        return tuple(_decode_required_run(row) for row in rows)

    async def claim(self, request: ClaimRequest) -> ClaimResult:
        checked = _checked_claim(request)

        async def operation(tx: DocumentStoreTransaction) -> ClaimResult:
            run = await self._tx_get_run(tx, tenant_id=checked.tenant_id, run_id=checked.run_id)
            if run is None:
                return ClaimResult(disposition=ClaimDisposition.NOT_FOUND, run=None)
            if run.state in TERMINAL_RUN_STATES:
                return ClaimResult(disposition=ClaimDisposition.TERMINAL, run=run)
            if checked.now < run.updated_at:
                raise ValueError("claim time cannot precede the stored update")
            if run.cancellation_requested_at is not None:
                if run.lease is not None and run.lease.expires_at > checked.now:
                    return ClaimResult(
                        disposition=ClaimDisposition.CANCELLATION_REQUESTED,
                        run=run,
                    )
                cancelled = _changed_run(
                    run,
                    state=RunState.CANCELLED,
                    updated_at=checked.now,
                    terminal_at=checked.now,
                    lease=None,
                )
                await self._save_run(tx, previous=run, changed=cancelled)
                await self._append_event(tx, cancelled)
                return ClaimResult(
                    disposition=ClaimDisposition.CANCELLATION_REQUESTED,
                    run=cancelled,
                )
            if run.lease is not None and run.lease.expires_at > checked.now:
                exact_owner = (
                    run.lease.lease_id == checked.lease_id
                    and run.lease.worker_id == checked.worker_id
                )
                return ClaimResult(
                    disposition=(
                        ClaimDisposition.ALREADY_CLAIMED
                        if exact_owner
                        else ClaimDisposition.BUSY
                    ),
                    run=run,
                )
            expires_at = _lease_expiry(checked.now, checked.lease_seconds)
            if expires_at is None:
                return ClaimResult(
                    disposition=ClaimDisposition.LEASE_UNAVAILABLE,
                    run=run,
                )
            claimed = _changed_run(
                run,
                state=RunState.RUNNING,
                updated_at=checked.now,
                delivery_attempts=run.delivery_attempts + 1,
                lease=ExecutionLease(
                    lease_id=checked.lease_id,
                    worker_id=checked.worker_id,
                    acquired_at=checked.now,
                    expires_at=expires_at,
                ),
            )
            await self._save_run(tx, previous=run, changed=claimed)
            await self._append_event(tx, claimed)
            return ClaimResult(disposition=ClaimDisposition.CLAIMED, run=claimed)

        return await self._store.transaction(operation)

    async def renew_lease(self, renewal: LeaseRenewal) -> LeaseResult:
        checked = _checked_renewal(renewal)

        async def operation(tx: DocumentStoreTransaction) -> LeaseResult:
            run = await self._tx_get_run(tx, tenant_id=checked.tenant_id, run_id=checked.run_id)
            if run is None:
                return LeaseResult(disposition=LeaseDisposition.NOT_FOUND, run=None)
            if run.state in TERMINAL_RUN_STATES:
                return LeaseResult(disposition=LeaseDisposition.TERMINAL, run=run)
            if run.cancellation_requested_at is not None:
                return LeaseResult(
                    disposition=LeaseDisposition.CANCELLATION_REQUESTED,
                    run=run,
                )
            lease = run.lease
            if (
                lease is None
                or lease.lease_id != checked.lease_id
                or lease.worker_id != checked.worker_id
                or lease.expires_at <= checked.now
                or checked.now < run.updated_at
            ):
                return LeaseResult(disposition=LeaseDisposition.LOST, run=run)
            requested_expiry = _lease_expiry(checked.now, checked.lease_seconds)
            if requested_expiry is None:
                return LeaseResult(
                    disposition=LeaseDisposition.LEASE_UNAVAILABLE, run=run
                )
            renewed = _changed_run(
                run,
                updated_at=checked.now,
                lease=ExecutionLease(
                    lease_id=lease.lease_id,
                    worker_id=lease.worker_id,
                    acquired_at=lease.acquired_at,
                    expires_at=max(lease.expires_at, requested_expiry),
                ),
            )
            await self._save_run(tx, previous=run, changed=renewed)
            return LeaseResult(disposition=LeaseDisposition.RENEWED, run=renewed)

        return await self._store.transaction(operation)

    async def compare_and_set(self, update: StateUpdate) -> StateUpdateResult:
        checked = _checked_update(update)

        async def operation(tx: DocumentStoreTransaction) -> StateUpdateResult:
            run = await self._tx_get_run(tx, tenant_id=checked.tenant_id, run_id=checked.run_id)
            if run is None:
                return StateUpdateResult(
                    disposition=WriteDisposition.NOT_FOUND,
                    run=None,
                )
            if (
                run.version != checked.expected_version
                or run.state is not checked.expected_state
                or checked.at < run.updated_at
            ):
                return StateUpdateResult(
                    disposition=WriteDisposition.CONFLICT,
                    run=run,
                )
            if checked.lease_id is not None and (
                run.lease is None
                or run.lease.lease_id != checked.lease_id
                or run.lease.worker_id != checked.worker_id
                or run.lease.expires_at <= checked.at
            ):
                return StateUpdateResult(
                    disposition=WriteDisposition.LEASE_LOST,
                    run=run,
                )
            if (
                run.cancellation_requested_at is not None
                and checked.next_state is not RunState.CANCELLED
            ):
                return StateUpdateResult(
                    disposition=WriteDisposition.CANCELLATION_REQUESTED,
                    run=run,
                )
            changed = _changed_run(
                run,
                state=checked.next_state,
                updated_at=checked.at,
                cancellation_requested_at=(
                    (run.cancellation_requested_at or checked.at)
                    if checked.next_state is RunState.CANCELLED
                    else run.cancellation_requested_at
                ),
                terminal_at=(
                    checked.at if checked.next_state in TERMINAL_RUN_STATES else None
                ),
                lease=(
                    None if checked.next_state in TERMINAL_RUN_STATES else run.lease
                ),
                answer=checked.answer,
                failure_code=checked.failure_code,
            )
            await self._save_run(tx, previous=run, changed=changed)
            await self._write_reflection(
                tx,
                reflection=checked.reflection,
                tenant_id=run.tenant_id,
                session_id=run.session_id,
                run_id=run.run_id,
            )
            await self._append_event(tx, changed)
            return StateUpdateResult(
                disposition=WriteDisposition.APPLIED,
                run=changed,
            )

        return await self._store.transaction(operation)

    async def request_cancellation(
        self, *, tenant_id: OpaqueId, run_id: OpaqueId, at: datetime
    ) -> CancellationResult:
        checked_tenant = _scope_id(tenant_id)
        checked_run = _scope_id(run_id)
        checked_at = _parse_timestamp(_timestamp(at))

        async def operation(tx: DocumentStoreTransaction) -> CancellationResult:
            run = await self._tx_get_run(tx, tenant_id=checked_tenant, run_id=checked_run)
            if run is None:
                return CancellationResult(run=None, changed=False)
            if run.state in TERMINAL_RUN_STATES or run.cancellation_requested_at is not None:
                return CancellationResult(run=run, changed=False)
            if checked_at < run.updated_at:
                raise ValueError("cancellation time cannot precede the stored update")
            immediate = run.state is RunState.QUEUED or (
                run.lease is not None and run.lease.expires_at <= checked_at
            )
            cancelled = _changed_run(
                run,
                state=RunState.CANCELLED if immediate else run.state,
                updated_at=checked_at,
                cancellation_requested_at=checked_at,
                terminal_at=checked_at if immediate else None,
                lease=None if immediate else run.lease,
            )
            await self._save_run(tx, previous=run, changed=cancelled)
            await self._append_event(
                tx,
                cancelled,
                message=(None if immediate else "Run cancellation was requested."),
            )
            return CancellationResult(run=cancelled, changed=True)

        return await self._store.transaction(operation)

    async def delete_run(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> bool:
        checked_tenant = _scope_id(tenant_id)
        checked_run = _scope_id(run_id)

        async def operation(tx: DocumentStoreTransaction) -> bool:
            run = await self._tx_get_run(tx, tenant_id=checked_tenant, run_id=checked_run)
            if run is None:
                return False
            return await self._delete_run_in_tx(tx, run=run)

        return await self._store.transaction(operation)

    async def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        checked_session = _scope_id(session_id)

        async def operation(tx: DocumentStoreTransaction) -> int:
            runs = await tx.list(
                collection=_RUNS,
                filters={
                    "tenant_id": checked_tenant,
                    "session_id": checked_session,
                },
            )
            deleted = 0
            for row in runs:
                deleted += int(await self._delete_run_in_tx(tx, run=_decode_required_run(row)))
            await tx.delete(
                collection=_SESSIONS,
                document_id=_document_id(checked_tenant, checked_session),
            )
            await _delete_documents(
                tx,
                collection=_REFLECTIONS,
                filters={"tenant_id": checked_tenant, "session_id": checked_session},
            )
            return deleted

        return await self._store.transaction(operation)

    async def delete_tenant(self, *, tenant_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)

        async def operation(tx: DocumentStoreTransaction) -> int:
            runs = await tx.list(
                collection=_RUNS,
                filters={"tenant_id": checked_tenant},
            )
            deleted = 0
            for row in runs:
                deleted += int(await self._delete_run_in_tx(tx, run=_decode_required_run(row)))
            await _delete_documents(
                tx, collection=_SESSIONS, filters={"tenant_id": checked_tenant}
            )
            await _delete_documents(
                tx, collection=_REFLECTIONS, filters={"tenant_id": checked_tenant}
            )
            return deleted

        return await self._store.transaction(operation)

    async def _tx_get_run(
        self, tx: DocumentStoreTransaction, *, tenant_id: str, run_id: str
    ) -> RunRecord | None:
        return _decode_run_document(
            await tx.get(collection=_RUNS, document_id=_document_id(tenant_id, run_id))
        )

    async def _save_run(
        self, tx: DocumentStoreTransaction, *, previous: RunRecord, changed: RunRecord
    ) -> None:
        if previous.version >= changed.version:
            raise StorageError("run version did not advance")
        await tx.set(
            collection=_RUNS,
            document_id=_document_id(previous.tenant_id, previous.run_id),
            document=_run_document(changed),
        )

    async def _delete_run_in_tx(
        self, tx: DocumentStoreTransaction, *, run: RunRecord
    ) -> bool:
        await tx.delete(
            collection=_IDEMPOTENCY,
            document_id=_document_id(run.tenant_id, run.idempotency_key),
        )
        await _delete_documents(
            tx,
            collection=_EVENTS,
            filters={"tenant_id": run.tenant_id, "run_id": run.run_id},
        )
        await tx.delete(
            collection=_REFLECTIONS,
            document_id=_document_id(run.tenant_id, run.session_id, run.run_id),
        )
        await _delete_documents(
            tx,
            collection=_WORK_ITEMS,
            filters={"tenant_id": run.tenant_id, "run_id": run.run_id},
        )
        return await tx.delete(
            collection=_RUNS,
            document_id=_document_id(run.tenant_id, run.run_id),
        )

    async def _append_event(
        self,
        tx: DocumentStoreTransaction,
        run: RunRecord,
        *,
        message: str | None = None,
    ) -> None:
        rows = await tx.list(
            collection=_EVENTS,
            filters={"tenant_id": run.tenant_id, "run_id": run.run_id},
            order_by=("sequence",),
        )
        sequence = 1 if not rows else int(rows[-1]["sequence"]) + 1
        failure = None if run.failure_code is None else public_run_failure(run.failure_code)
        event = RunEvent(
            sequence=sequence,
            run_id=run.run_id,
            event_type=_RUN_EVENT_TYPES.get(run.state, RunEventType.STATUS),
            state=run.state,
            occurred_at=run.updated_at,
            message=(
                message
                or (failure.message if failure is not None else _RUN_EVENT_MESSAGES[run.state])
            ),
            answer=run.answer,
            failure=failure,
        )
        await tx.set(
            collection=_EVENTS,
            document_id=_document_id(run.tenant_id, run.run_id, str(event.sequence)),
            document=_event_document(run.tenant_id, event),
        )

    async def _write_reflection(
        self,
        tx: DocumentStoreTransaction,
        *,
        reflection: RunReflection | None,
        tenant_id: str,
        session_id: str,
        run_id: str,
    ) -> None:
        if reflection is None:
            return
        if (
            reflection.tenant_id != tenant_id
            or reflection.session_id != session_id
            or reflection.run_id != run_id
        ):
            raise ValueError("reflection must match the run scope")
        await tx.set(
            collection=_REFLECTIONS,
            document_id=_document_id(tenant_id, session_id, run_id),
            document={
                "document_id": _document_id(tenant_id, session_id, run_id),
                "tenant_id": tenant_id,
                "session_id": session_id,
                "run_id": run_id,
                "payload": reflection.model_dump_json(),
            },
        )


class FirestoreEventRepository:
    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    async def append(self, *, tenant_id: OpaqueId, event: RunEvent) -> bool:
        checked_tenant = _scope_id(tenant_id)
        checked = RunEvent.model_validate(event.model_dump(mode="python"))

        async def operation(tx: DocumentStoreTransaction) -> bool:
            document_id = _document_id(checked_tenant, checked.run_id, str(checked.sequence))
            existing = await tx.get(collection=_EVENTS, document_id=document_id)
            if existing is not None:
                if _decode_event_document(existing) != checked:
                    raise StorageConflictError("event sequence already exists")
                return False
            rows = await tx.list(
                collection=_EVENTS,
                filters={"tenant_id": checked_tenant, "run_id": checked.run_id},
                order_by=("sequence",),
            )
            if rows:
                latest = _decode_event_document(rows[-1])
                if checked.sequence <= latest.sequence:
                    raise QueueConflictError("event sequence must increase")
                if latest.event_type is not RunEventType.STATUS:
                    raise StorageConflictError("terminal event must remain final")
            run = _decode_run_document(
                await tx.get(
                    collection=_RUNS,
                    document_id=_document_id(checked_tenant, checked.run_id),
                )
            )
            if run is None:
                raise StorageConflictError("event run does not exist")
            expected_failure = (
                None if run.failure_code is None else public_run_failure(run.failure_code)
            )
            if (
                checked.state is not run.state
                or checked.answer != run.answer
                or checked.failure != expected_failure
            ):
                raise StorageConflictError("event does not match run state")
            await tx.set(
                collection=_EVENTS,
                document_id=document_id,
                document=_event_document(checked_tenant, checked),
            )
            return True

        try:
            return await self._store.transaction(operation)
        except StorageConflictError:
            raise

    async def list(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[RunEvent, ...]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("event sequence must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        rows = await self._store.list(
            collection=_EVENTS,
            filters={"tenant_id": _scope_id(tenant_id), "run_id": _scope_id(run_id)},
            order_by=("sequence",),
        )
        return tuple(
            _decode_event_document(row)
            for row in rows
            if int(row["sequence"]) > after_sequence
        )[:limit]


class CloudTasksWorkQueue:
    """Task dispatch with a document index for cancel and duplicate repair."""

    def __init__(
        self,
        *,
        store: DocumentStore,
        task_client: CloudTaskClient,
        queue_name: str,
        codec: SignedWorkItemCodec,
    ) -> None:
        self._store = store
        self._tasks = task_client
        self._queue_name = queue_name
        self._codec = codec

    async def enqueue(self, item: WorkItem) -> EnqueueResult:
        checked = _checked_item(item)
        existing = await self._store.get(
            collection=_WORK_ITEMS,
            document_id=_document_id(checked.work_id),
        )
        if existing is not None:
            stored = _decode_work_document(existing)
            if (stored.tenant_id, stored.run_id) != (checked.tenant_id, checked.run_id):
                raise QueueConflictError("work id already identifies another run")
            return EnqueueResult(item=stored, created=False)
        if await self._store.get(
            collection=_RUNS,
            document_id=_document_id(checked.tenant_id, checked.run_id),
        ) is None:
            raise RunParentNotFoundError("work item run does not exist")
        task_name = self._task_name(checked.work_id)
        body, headers = self._codec.encode(
            checked, task_name=task_name, queue_name=self._queue_name
        )
        try:
            await self._tasks.create(
                CloudTask(
                    name=task_name,
                    schedule_at=checked.not_before,
                    body=body,
                    headers=headers,
                )
            )
        except CloudTaskAlreadyExistsError as err:
            remote = await self._tasks.get(name=task_name)
            if remote is None:
                raise StorageError("remote task disappeared during enqueue") from err
            decoded = self._codec.decode(
                body=remote.body,
                signature=_header_value(remote.headers, _SIGNATURE_HEADER),
                task_name=remote.name,
                queue_name=self._queue_name,
            )
            if (decoded.tenant_id, decoded.run_id) != (checked.tenant_id, checked.run_id):
                raise QueueConflictError(
                    "work id already identifies another run"
                ) from err
            await self._store.transaction(
                lambda tx: tx.set(
                    collection=_WORK_ITEMS,
                    document_id=_document_id(decoded.work_id),
                    document=_work_document(decoded, task_name=task_name),
                )
            )
            return EnqueueResult(item=decoded, created=False)
        await self._store.transaction(
            lambda tx: tx.set(
                collection=_WORK_ITEMS,
                document_id=_document_id(checked.work_id),
                document=_work_document(checked, task_name=task_name),
            )
        )
        return EnqueueResult(item=checked, created=True)

    async def cancel(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        checked_run = _scope_id(run_id)
        rows = await self._store.list(
            collection=_WORK_ITEMS,
            filters={"tenant_id": checked_tenant, "run_id": checked_run},
        )
        removed = 0
        for row in rows:
            task_name = _require_text(row, "task_name")
            document_id = _require_text(row, "document_id")
            deleted = await self._tasks.delete(name=task_name)
            if (
                (deleted or await self._tasks.get(name=task_name) is None)
                and await self._store.transaction(
                    lambda tx, document_id=document_id: tx.delete(
                        collection=_WORK_ITEMS,
                        document_id=document_id,
                    )
                )
            ):
                removed += 1
        return removed

    def decode_delivery(
        self,
        *,
        body: bytes,
        signature: str | None,
        task_name: str | None,
        queue_name: str | None,
    ) -> WorkItem:
        if queue_name != self._queue_name:
            raise TaskDeliveryAuthError("task delivery queue is invalid")
        item = self._codec.decode(
            body=body,
            signature=signature,
            task_name=task_name,
            queue_name=queue_name,
        )
        if task_name != self._task_name(item.work_id):
            raise TaskDeliveryAuthError("task delivery name is invalid")
        return item

    def _task_name(self, work_id: str) -> str:
        suffix = hashlib.sha256(work_id.encode("utf-8")).hexdigest()[:32]
        return f"{self._queue_name}/tasks/work-{suffix}"


def _document_id(*parts: str) -> str:
    return "|".join(parts)


def _require_delivery_identity(
    *, task_name: str | None, queue_name: str | None
) -> None:
    if (
        type(task_name) is not str
        or not task_name
        or type(queue_name) is not str
        or not queue_name
    ):
        raise ValueError("task and queue names must be non-empty strings")


def _delivery_signature_input(
    *, body: bytes, task_name: str, queue_name: str
) -> bytes:
    return b"\x00".join(
        (queue_name.encode("utf-8"), task_name.encode("utf-8"), body)
    )


def _require_text(document: Mapping[str, object], field: str) -> str:
    value = document.get(field)
    if type(value) is not str:
        raise StorageError(f"{field} is not stored as text")
    return value


def _run_document(run: RunRecord) -> dict[str, object]:
    return {
        "document_id": _document_id(run.tenant_id, run.run_id),
        "tenant_id": run.tenant_id,
        "session_id": run.session_id,
        "run_id": run.run_id,
        "state": run.state.value,
        "version": run.version,
        "idempotency_key": run.idempotency_key,
        "created_at": _timestamp(run.created_at),
        "updated_at": _timestamp(run.updated_at),
        "payload": run.model_dump_json(exclude_none=True),
    }


def _decode_run_document(document: Mapping[str, object] | None) -> RunRecord | None:
    if document is None:
        return None
    try:
        payload = json.loads(_require_text(document, "payload"))
        if type(payload) is not dict:
            raise ValueError("run payload is not an object")
        for field in (
            "created_at",
            "updated_at",
            "cancellation_requested_at",
            "terminal_at",
        ):
            if payload.get(field) is not None:
                payload[field] = _parse_timestamp(payload[field])
        if payload.get("answer") is not None:
            payload["answer"] = ScopedAnswer.model_validate_json(
                json.dumps(payload["answer"], ensure_ascii=False)
            )
        if payload.get("failure_code") is not None:
            payload["failure_code"] = RunFailureCode(payload["failure_code"])
        lease = payload.get("lease")
        if lease is not None:
            if type(lease) is not dict:
                raise ValueError("run lease is not an object")
            lease["acquired_at"] = _parse_timestamp(lease.get("acquired_at"))
            lease["expires_at"] = _parse_timestamp(lease.get("expires_at"))
        run = RunRecord.model_validate(payload, strict=False)
        checked = _checked_run(run)
        expected = (
            checked.tenant_id,
            checked.run_id,
            checked.session_id,
            checked.state.value,
            checked.idempotency_key,
            _timestamp(checked.created_at),
            _timestamp(checked.updated_at),
        )
        actual = (
            _require_text(document, "tenant_id"),
            _require_text(document, "run_id"),
            _require_text(document, "session_id"),
            checked.state.value if document.get("state") is None else _require_text(document, "state"),
            _require_text(document, "idempotency_key"),
            _require_text(document, "created_at"),
            _require_text(document, "updated_at"),
        )
        if expected != actual:
            raise ValueError("indexed run fields disagree with payload")
        return checked
    except (TypeError, ValueError) as exc:
        raise StorageError("stored run failed validation") from exc


def _decode_required_run(document: Mapping[str, object]) -> RunRecord:
    run = _decode_run_document(document)
    if run is None:
        raise StorageError("stored run is missing")
    return run


def _event_document(tenant_id: str, event: RunEvent) -> dict[str, object]:
    return {
        "document_id": _document_id(tenant_id, event.run_id, str(event.sequence)),
        "tenant_id": tenant_id,
        "run_id": event.run_id,
        "sequence": event.sequence,
        "occurred_at": _timestamp(event.occurred_at),
        "payload": event.model_dump_json(exclude_none=True),
    }


def _decode_event_document(document: Mapping[str, object]) -> RunEvent:
    try:
        payload = json.loads(_require_text(document, "payload"))
        if type(payload) is not dict:
            raise ValueError("event payload is not an object")
        payload["occurred_at"] = _parse_timestamp(payload.get("occurred_at"))
        if payload.get("answer") is not None:
            payload["answer"] = ScopedAnswer.model_validate_json(
                json.dumps(payload["answer"], ensure_ascii=False)
            )
        if payload.get("failure") is not None:
            payload["failure"] = RunFailure.model_validate_json(
                json.dumps(payload["failure"], ensure_ascii=False)
            )
        parsed = RunEvent.model_validate(payload, strict=False)
        event = RunEvent.model_validate(parsed.model_dump(mode="python"))
    except (TypeError, ValueError) as exc:
        raise StorageError("stored event failed validation") from exc
    if (
        _require_text(document, "run_id") != event.run_id
        or int(document["sequence"]) != event.sequence
        or _parse_timestamp(_require_text(document, "occurred_at")) != event.occurred_at
    ):
        raise StorageError("indexed event fields disagree with payload")
    return event


def _work_document(item: WorkItem, *, task_name: str) -> dict[str, object]:
    return {
        "document_id": _document_id(item.work_id),
        "work_id": item.work_id,
        "tenant_id": item.tenant_id,
        "run_id": item.run_id,
        "task_name": task_name,
        "payload": item.model_dump_json(),
    }


def _decode_work_document(document: Mapping[str, object]) -> WorkItem:
    try:
        return WorkItem.model_validate_json(_require_text(document, "payload"))
    except ValueError as exc:
        raise StorageError("stored work item failed validation") from exc


async def _delete_documents(
    tx: DocumentStoreTransaction,
    *,
    collection: str,
    filters: Mapping[str, object],
) -> None:
    for row in await tx.list(collection=collection, filters=filters):
        await tx.delete(collection=collection, document_id=_require_text(row, "document_id"))


def _header_value(headers: tuple[tuple[str, str], ...], name: str) -> str | None:
    matches = [value for key, value in headers if key.lower() == name.lower()]
    return matches[0] if len(matches) == 1 else None


__all__ = [
    "CloudTask",
    "CloudTaskAlreadyExistsError",
    "CloudTaskClient",
    "CloudTasksWorkQueue",
    "DocumentStore",
    "DocumentStoreTransaction",
    "FirestoreEventRepository",
    "FirestoreRunRepository",
    "SignedWorkItemCodec",
    "TaskDeliveryAuthError",
]
