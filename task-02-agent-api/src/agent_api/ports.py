"""Durable behavior contracts shared by local and cloud API adapters."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import Field, StringConstraints, field_validator, model_validator

from search_agent.contracts import OpaqueId, QueryText, StrictModel
from search_agent.memory import ReflectionRepository, RunReflection

IdempotencyKey = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9]+(?:[._~-][A-Za-z0-9]+)*$",
    ),
]


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_RUN_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.EXPIRED}
)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def _require_optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _require_utc(value)


class RunSubmission(StrictModel):
    """One accepted request; idempotency is scoped by ``(tenant_id, key)``."""

    tenant_id: OpaqueId
    session_id: OpaqueId
    run_id: OpaqueId
    idempotency_key: IdempotencyKey
    query: QueryText
    created_at: datetime

    _created_at_is_utc = field_validator("created_at")(_require_utc)


class ExecutionLease(StrictModel):
    lease_id: OpaqueId
    worker_id: OpaqueId
    acquired_at: datetime
    expires_at: datetime

    _timestamps_are_utc = field_validator("acquired_at", "expires_at")(_require_utc)

    @model_validator(mode="after")
    def validate_interval(self) -> ExecutionLease:
        if self.expires_at <= self.acquired_at:
            raise ValueError("lease expiry must follow acquisition")
        return self


class RunRecord(StrictModel):
    tenant_id: OpaqueId
    session_id: OpaqueId
    run_id: OpaqueId
    idempotency_key: IdempotencyKey
    query: QueryText
    state: RunState
    version: int = Field(ge=0, le=1_000_000)
    delivery_attempts: int = Field(ge=0, le=1_000)
    created_at: datetime
    updated_at: datetime
    cancellation_requested_at: datetime | None = None
    terminal_at: datetime | None = None
    lease: ExecutionLease | None = None

    _required_timestamps_are_utc = field_validator("created_at", "updated_at")(
        _require_utc
    )
    _optional_timestamps_are_utc = field_validator(
        "cancellation_requested_at", "terminal_at"
    )(_require_optional_utc)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> RunRecord:
        if self.updated_at < self.created_at:
            raise ValueError("run update cannot precede creation")
        if self.cancellation_requested_at is not None and not (
            self.created_at <= self.cancellation_requested_at <= self.updated_at
        ):
            raise ValueError("cancellation timestamp must be within the run lifetime")
        is_terminal = self.state in TERMINAL_RUN_STATES
        if is_terminal != (self.terminal_at is not None):
            raise ValueError("terminal timestamp must match terminal state")
        if self.terminal_at is not None and not (
            self.created_at <= self.terminal_at <= self.updated_at
        ):
            raise ValueError("terminal timestamp must be within the run lifetime")
        if is_terminal and self.lease is not None:
            raise ValueError("terminal runs cannot retain an execution lease")
        if (
            is_terminal
            and self.cancellation_requested_at is not None
            and self.state is not RunState.CANCELLED
        ):
            raise ValueError("a requested cancellation cannot end in another state")
        if self.state is RunState.QUEUED and self.lease is not None:
            raise ValueError("queued runs cannot have an execution lease")
        is_worker_owned = self.state in {
            RunState.RUNNING,
            RunState.WAITING_FOR_TOOL,
        }
        if is_worker_owned != (self.lease is not None):
            raise ValueError("worker-owned states require an execution lease")
        if is_worker_owned and self.delivery_attempts == 0:
            raise ValueError("worker-owned states require a delivery attempt")
        if self.lease is not None and not (
            self.created_at <= self.lease.acquired_at <= self.updated_at
        ):
            raise ValueError("lease acquisition must be within the run lifetime")
        return self


class CreateRunResult(StrictModel):
    run: RunRecord
    created: bool


class ClaimRequest(StrictModel):
    tenant_id: OpaqueId
    run_id: OpaqueId
    worker_id: OpaqueId
    lease_id: OpaqueId
    now: datetime
    lease_seconds: int = Field(ge=1, le=900)

    _now_is_utc = field_validator("now")(_require_utc)


class ClaimDisposition(StrEnum):
    CLAIMED = "claimed"
    ALREADY_CLAIMED = "already_claimed"
    BUSY = "busy"
    CANCELLATION_REQUESTED = "cancellation_requested"
    TERMINAL = "terminal"
    NOT_FOUND = "not_found"


class ClaimResult(StrictModel):
    disposition: ClaimDisposition
    run: RunRecord | None

    @model_validator(mode="after")
    def validate_result(self) -> ClaimResult:
        if (self.disposition is ClaimDisposition.NOT_FOUND) != (self.run is None):
            raise ValueError("only a missing run may omit the run record")
        if self.disposition in {
            ClaimDisposition.CLAIMED,
            ClaimDisposition.ALREADY_CLAIMED,
        } and (
            self.run is None
            or self.run.lease is None
            or self.run.state is not RunState.RUNNING
        ):
            raise ValueError("successful claims require a lease")
        return self


class LeaseRenewal(StrictModel):
    tenant_id: OpaqueId
    run_id: OpaqueId
    worker_id: OpaqueId
    lease_id: OpaqueId
    now: datetime
    lease_seconds: int = Field(ge=1, le=900)

    _now_is_utc = field_validator("now")(_require_utc)


class LeaseDisposition(StrEnum):
    RENEWED = "renewed"
    LOST = "lost"
    CANCELLATION_REQUESTED = "cancellation_requested"
    TERMINAL = "terminal"
    NOT_FOUND = "not_found"


class LeaseResult(StrictModel):
    disposition: LeaseDisposition
    run: RunRecord | None

    @model_validator(mode="after")
    def validate_result(self) -> LeaseResult:
        if (self.disposition is LeaseDisposition.NOT_FOUND) != (self.run is None):
            raise ValueError("only a missing run may omit the run record")
        if self.disposition is LeaseDisposition.RENEWED and (
            self.run is None or self.run.lease is None
        ):
            raise ValueError("a renewed run requires a lease")
        return self


_STATE_TRANSITIONS = {
    RunState.QUEUED: frozenset({RunState.FAILED, RunState.EXPIRED}),
    RunState.RUNNING: frozenset(
        {
            RunState.WAITING_FOR_TOOL,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.EXPIRED,
        }
    ),
    RunState.WAITING_FOR_TOOL: frozenset(
        {
            RunState.RUNNING,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.EXPIRED,
        }
    ),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.EXPIRED: frozenset(),
}


class StateUpdate(StrictModel):
    """One atomic compare-and-set transition against state, version, and lease."""

    tenant_id: OpaqueId
    run_id: OpaqueId
    expected_version: int = Field(ge=0, le=1_000_000)
    expected_state: RunState
    next_state: RunState
    at: datetime
    lease_id: OpaqueId | None = None

    _at_is_utc = field_validator("at")(_require_utc)

    @model_validator(mode="after")
    def validate_transition(self) -> StateUpdate:
        if self.next_state not in _STATE_TRANSITIONS[self.expected_state]:
            raise ValueError("illegal run state transition")
        owned_state = self.expected_state in {
            RunState.RUNNING,
            RunState.WAITING_FOR_TOOL,
        }
        if owned_state != (self.lease_id is not None):
            raise ValueError("worker-owned transitions require exactly one lease id")
        return self


class WriteDisposition(StrEnum):
    APPLIED = "applied"
    CONFLICT = "conflict"
    LEASE_LOST = "lease_lost"
    CANCELLATION_REQUESTED = "cancellation_requested"
    NOT_FOUND = "not_found"


class StateUpdateResult(StrictModel):
    disposition: WriteDisposition
    run: RunRecord | None

    @model_validator(mode="after")
    def validate_result(self) -> StateUpdateResult:
        if (self.disposition is WriteDisposition.NOT_FOUND) != (self.run is None):
            raise ValueError("only a missing run may omit the run record")
        return self


class CancellationResult(StrictModel):
    run: RunRecord | None
    changed: bool

    @model_validator(mode="after")
    def validate_result(self) -> CancellationResult:
        if self.run is None and self.changed:
            raise ValueError("a missing run cannot be changed")
        if self.changed and (
            self.run is None or self.run.cancellation_requested_at is None
        ):
            raise ValueError("a changed run requires a cancellation timestamp")
        return self


class RunRepository(Protocol):
    """Strongly visible, tenant-scoped run state with atomic write operations.

    Idempotency keys are unique per tenant. A retry with the same session and query
    returns the first record even when the proposed run id or timestamp differs;
    different request content raises :class:`IdempotencyConflictError`.
    """

    async def create(self, submission: RunSubmission) -> CreateRunResult: ...

    async def get(
        self, *, tenant_id: OpaqueId, run_id: OpaqueId
    ) -> RunRecord | None: ...

    async def list_session(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, limit: int = 100
    ) -> tuple[RunRecord, ...]:
        """Return at most ``limit`` records ordered by ``(created_at, run_id)``."""

        ...

    async def claim(self, request: ClaimRequest) -> ClaimResult:
        """Claim queued or expired work; an active lease permits no second owner."""

        ...

    async def renew_lease(self, renewal: LeaseRenewal) -> LeaseResult: ...

    async def compare_and_set(self, update: StateUpdate) -> StateUpdateResult: ...

    async def request_cancellation(
        self, *, tenant_id: OpaqueId, run_id: OpaqueId, at: datetime
    ) -> CancellationResult:
        """Cancel queued work immediately or flag leased work exactly once."""

        ...

    async def delete_run(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> bool: ...

    async def delete_session(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId
    ) -> int: ...

    async def delete_tenant(self, *, tenant_id: OpaqueId) -> int: ...


class WorkItem(StrictModel):
    """An idempotently named queue reference; authoritative state stays in storage."""

    work_id: OpaqueId
    tenant_id: OpaqueId
    run_id: OpaqueId
    enqueued_at: datetime
    not_before: datetime

    _timestamps_are_utc = field_validator("enqueued_at", "not_before")(_require_utc)

    @model_validator(mode="after")
    def validate_schedule(self) -> WorkItem:
        if self.not_before < self.enqueued_at:
            raise ValueError("work cannot become available before enqueue")
        return self


class EnqueueResult(StrictModel):
    item: WorkItem
    created: bool


class WorkQueue(Protocol):
    """At-least-once dispatch boundary for local queues and Cloud Tasks.

    Dispatch order is deliberately not a correctness guarantee. Duplicate delivery
    is resolved by ``RunRepository.claim``; ``work_id`` only makes enqueue retries
    idempotent. Cancellation removes undispatched work, while a concurrent dispatch
    is stopped by the repository cancellation flag.
    """

    async def enqueue(self, item: WorkItem) -> EnqueueResult: ...

    async def cancel(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> int: ...


class IdempotencyConflictError(ValueError):
    """The same tenant key was reused for different request content."""


class QueueConflictError(ValueError):
    """The same work id was reused for a different tenant or run."""


__all__ = [
    "TERMINAL_RUN_STATES",
    "CancellationResult",
    "ClaimDisposition",
    "ClaimRequest",
    "ClaimResult",
    "CreateRunResult",
    "EnqueueResult",
    "ExecutionLease",
    "IdempotencyConflictError",
    "IdempotencyKey",
    "LeaseDisposition",
    "LeaseRenewal",
    "LeaseResult",
    "QueueConflictError",
    "ReflectionRepository",
    "RunRecord",
    "RunReflection",
    "RunRepository",
    "RunState",
    "RunSubmission",
    "StateUpdate",
    "StateUpdateResult",
    "WorkItem",
    "WorkQueue",
    "WriteDisposition",
]
