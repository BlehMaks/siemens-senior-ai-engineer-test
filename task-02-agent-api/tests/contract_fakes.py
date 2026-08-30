"""Deterministic adapters used to execute the reusable P00 behavior contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from pydantic import TypeAdapter

from agent_api.ports import (
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
    RunRecord,
    RunState,
    RunSubmission,
    StateUpdate,
    StateUpdateResult,
    WorkItem,
    WriteDisposition,
)
from search_agent.contracts import OpaqueId, StrictModel

_OPAQUE_ID = TypeAdapter(OpaqueId)


def _checked[ModelT: StrictModel](model_type: type[ModelT], value: ModelT) -> ModelT:
    if type(value) is not model_type:
        raise ValueError("contract value has the wrong concrete type")
    return model_type.model_validate(value.model_dump(mode="python"))


def _scope_id(value: OpaqueId) -> OpaqueId:
    if type(value) is not str:
        raise ValueError("scope id must be a string")
    return _OPAQUE_ID.validate_python(value, strict=True)


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise ValueError("timestamp must be an exact datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def _lease_expiry(now: datetime, lease_seconds: int) -> datetime | None:
    lease_delta = timedelta(seconds=lease_seconds)
    last_instant = datetime.max.replace(tzinfo=now.tzinfo)
    if last_instant - now < lease_delta:
        return None
    return now + lease_delta


class FakeRunRepository:
    """In-memory reference behavior, not authoritative application storage."""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], RunRecord] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, str, str]] = {}
        self._lock = asyncio.Lock()

    async def create(self, submission: RunSubmission) -> CreateRunResult:
        checked = _checked(RunSubmission, submission)
        key = (checked.tenant_id, checked.idempotency_key)
        async with self._lock:
            existing_identity = self._idempotency.get(key)
            if existing_identity is not None:
                session_id, query, run_id = existing_identity
                if (session_id, query) != (checked.session_id, checked.query):
                    raise IdempotencyConflictError(
                        "idempotency key already identifies another request"
                    )
                return CreateRunResult(
                    run=self._runs[(checked.tenant_id, run_id)], created=False
                )
            run_key = (checked.tenant_id, checked.run_id)
            if run_key in self._runs:
                raise IdempotencyConflictError("run id already exists")
            run = RunRecord(
                **checked.model_dump(mode="python", exclude={"created_at"}),
                state=RunState.QUEUED,
                version=0,
                delivery_attempts=0,
                created_at=checked.created_at,
                updated_at=checked.created_at,
            )
            self._runs[run_key] = run
            self._idempotency[key] = (run.session_id, run.query, run.run_id)
            return CreateRunResult(run=run, created=True)

    async def get(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> RunRecord | None:
        key = (_scope_id(tenant_id), _scope_id(run_id))
        async with self._lock:
            return self._runs.get(key)

    async def list_session(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, limit: int = 100
    ) -> tuple[RunRecord, ...]:
        checked_tenant = _scope_id(tenant_id)
        checked_session = _scope_id(session_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        async with self._lock:
            selected = [
                run
                for (run_tenant, _), run in self._runs.items()
                if (run_tenant, run.session_id) == (checked_tenant, checked_session)
            ]
            selected.sort(key=lambda run: (run.created_at, run.run_id))
            return tuple(selected[:limit])

    async def claim(self, request: ClaimRequest) -> ClaimResult:
        checked = _checked(ClaimRequest, request)
        key = (checked.tenant_id, checked.run_id)
        async with self._lock:
            run = self._runs.get(key)
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
                cancelled = run.model_copy(
                    update={
                        "state": RunState.CANCELLED,
                        "version": run.version + 1,
                        "updated_at": checked.now,
                        "terminal_at": checked.now,
                        "lease": None,
                    }
                )
                cancelled = RunRecord.model_validate(
                    cancelled.model_dump(mode="python")
                )
                self._runs[key] = cancelled
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
                    disposition=ClaimDisposition.LEASE_UNAVAILABLE, run=run
                )
            lease = ExecutionLease(
                lease_id=checked.lease_id,
                worker_id=checked.worker_id,
                acquired_at=checked.now,
                expires_at=expires_at,
            )
            claimed = run.model_copy(
                update={
                    "state": RunState.RUNNING,
                    "version": run.version + 1,
                    "delivery_attempts": run.delivery_attempts + 1,
                    "updated_at": checked.now,
                    "lease": lease,
                }
            )
            claimed = RunRecord.model_validate(claimed.model_dump(mode="python"))
            self._runs[key] = claimed
            return ClaimResult(disposition=ClaimDisposition.CLAIMED, run=claimed)

    async def renew_lease(self, renewal: LeaseRenewal) -> LeaseResult:
        checked = _checked(LeaseRenewal, renewal)
        key = (checked.tenant_id, checked.run_id)
        async with self._lock:
            run = self._runs.get(key)
            if run is None:
                return LeaseResult(disposition=LeaseDisposition.NOT_FOUND, run=None)
            if run.state in TERMINAL_RUN_STATES:
                return LeaseResult(disposition=LeaseDisposition.TERMINAL, run=run)
            if run.cancellation_requested_at is not None:
                return LeaseResult(
                    disposition=LeaseDisposition.CANCELLATION_REQUESTED, run=run
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
            renewed_lease = lease.model_copy(
                update={
                    "expires_at": max(
                        lease.expires_at,
                        requested_expiry,
                    )
                }
            )
            renewed = run.model_copy(
                update={
                    "version": run.version + 1,
                    "updated_at": checked.now,
                    "lease": renewed_lease,
                }
            )
            renewed = RunRecord.model_validate(renewed.model_dump(mode="python"))
            self._runs[key] = renewed
            return LeaseResult(disposition=LeaseDisposition.RENEWED, run=renewed)

    async def compare_and_set(self, update: StateUpdate) -> StateUpdateResult:
        checked = _checked(StateUpdate, update)
        key = (checked.tenant_id, checked.run_id)
        async with self._lock:
            run = self._runs.get(key)
            if run is None:
                return StateUpdateResult(
                    disposition=WriteDisposition.NOT_FOUND, run=None
                )
            if (
                run.version != checked.expected_version
                or run.state is not checked.expected_state
                or checked.at < run.updated_at
            ):
                return StateUpdateResult(disposition=WriteDisposition.CONFLICT, run=run)
            if checked.lease_id is not None and (
                run.lease is None
                or run.lease.lease_id != checked.lease_id
                or run.lease.worker_id != checked.worker_id
                or run.lease.expires_at <= checked.at
            ):
                return StateUpdateResult(
                    disposition=WriteDisposition.LEASE_LOST, run=run
                )
            if (
                run.cancellation_requested_at is not None
                and checked.next_state is not RunState.CANCELLED
            ):
                return StateUpdateResult(
                    disposition=WriteDisposition.CANCELLATION_REQUESTED,
                    run=run,
                )
            terminal = checked.next_state in TERMINAL_RUN_STATES
            next_run = run.model_copy(
                update={
                    "state": checked.next_state,
                    "version": run.version + 1,
                    "updated_at": checked.at,
                    "cancellation_requested_at": (
                        (run.cancellation_requested_at or checked.at)
                        if checked.next_state is RunState.CANCELLED
                        else run.cancellation_requested_at
                    ),
                    "terminal_at": checked.at if terminal else None,
                    "lease": None if terminal else run.lease,
                    "answer": checked.answer,
                    "failure_code": checked.failure_code,
                }
            )
            next_run = RunRecord.model_validate(next_run.model_dump(mode="python"))
            self._runs[key] = next_run
            return StateUpdateResult(disposition=WriteDisposition.APPLIED, run=next_run)

    async def request_cancellation(
        self, *, tenant_id: OpaqueId, run_id: OpaqueId, at: datetime
    ) -> CancellationResult:
        key = (_scope_id(tenant_id), _scope_id(run_id))
        checked_at = _utc(at)
        async with self._lock:
            run = self._runs.get(key)
            if run is None:
                return CancellationResult(run=None, changed=False)
            if run.state in TERMINAL_RUN_STATES or run.cancellation_requested_at:
                return CancellationResult(run=run, changed=False)
            if checked_at < run.updated_at:
                raise ValueError("cancellation time cannot precede the stored update")
            immediate = run.state is RunState.QUEUED or (
                run.lease is not None and run.lease.expires_at <= checked_at
            )
            cancelled = run.model_copy(
                update={
                    "state": RunState.CANCELLED if immediate else run.state,
                    "version": run.version + 1,
                    "updated_at": checked_at,
                    "cancellation_requested_at": checked_at,
                    "terminal_at": checked_at if immediate else None,
                    "lease": None if immediate else run.lease,
                }
            )
            cancelled = RunRecord.model_validate(cancelled.model_dump(mode="python"))
            self._runs[key] = cancelled
            return CancellationResult(run=cancelled, changed=True)

    async def delete_run(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> bool:
        key = (_scope_id(tenant_id), _scope_id(run_id))
        async with self._lock:
            run = self._runs.pop(key, None)
            if run is None:
                return False
            self._idempotency.pop((run.tenant_id, run.idempotency_key), None)
            return True

    async def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        checked_session = _scope_id(session_id)
        async with self._lock:
            keys = [
                key
                for key, run in self._runs.items()
                if (run.tenant_id, run.session_id) == (checked_tenant, checked_session)
            ]
            self._delete_keys(keys)
            return len(keys)

    async def delete_tenant(self, *, tenant_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        async with self._lock:
            keys = [key for key in self._runs if key[0] == checked_tenant]
            self._delete_keys(keys)
            return len(keys)

    def _delete_keys(self, keys: list[tuple[str, str]]) -> None:
        for key in keys:
            run = self._runs.pop(key)
            self._idempotency.pop((run.tenant_id, run.idempotency_key), None)


class FakeWorkQueue:
    def __init__(self) -> None:
        self._items: dict[str, WorkItem] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, item: WorkItem) -> EnqueueResult:
        checked = _checked(WorkItem, item)
        async with self._lock:
            existing = self._items.get(checked.work_id)
            if existing is not None:
                if (existing.tenant_id, existing.run_id) != (
                    checked.tenant_id,
                    checked.run_id,
                ):
                    raise QueueConflictError("work id already identifies another run")
                return EnqueueResult(item=existing, created=False)
            self._items[checked.work_id] = checked
            return EnqueueResult(item=checked, created=True)

    async def cancel(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        generation_id: OpaqueId | None = None,
    ) -> int:
        checked_tenant = _scope_id(tenant_id)
        checked_run = _scope_id(run_id)
        async with self._lock:
            keys = [
                work_id
                for work_id, item in self._items.items()
                if (item.tenant_id, item.run_id) == (checked_tenant, checked_run)
                and (generation_id is None or item.generation_id == generation_id)
            ]
            for work_id in keys:
                del self._items[work_id]
            return len(keys)

    async def discard(self, item: WorkItem) -> bool:
        checked = _checked(WorkItem, item)
        async with self._lock:
            if self._items.get(checked.work_id) != checked:
                return False
            del self._items[checked.work_id]
            return True

    async def ordered_items(self) -> tuple[WorkItem, ...]:
        """Test-only view; providers are not required to dispatch in this order."""

        async with self._lock:
            return tuple(
                sorted(
                    self._items.values(),
                    key=lambda item: (item.not_before, item.enqueued_at, item.work_id),
                )
            )
