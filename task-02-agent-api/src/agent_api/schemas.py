"""Strict public HTTP and SSE schemas for the versioned Agent API."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from search_agent.contracts import OpaqueId, QueryText, ScopedAnswer, StrictModel
from search_agent.memory.contracts import contains_sensitive_memory_text

from .ports import TERMINAL_RUN_STATES, RunState

SessionLabel = Annotated[
    str, StringConstraints(min_length=1, max_length=80, strip_whitespace=True)
]
PageCursor = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=256,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]
LastEventId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=19, pattern=r"^[1-9][0-9]{0,18}$"),
]
SafeMessage = Annotated[
    str, StringConstraints(min_length=1, max_length=240, strip_whitespace=True)
]
FieldPath = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$",
    ),
]

_LAST_EVENT_ID = TypeAdapter(LastEventId)
_MAX_EVENT_SEQUENCE = 9_223_372_036_854_775_807


def _require_utc(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise ValueError("timestamp must be an exact datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def _require_optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _require_utc(value)


def _revalidate_answer(value: object) -> ScopedAnswer:
    if isinstance(value, dict):
        answer = ScopedAnswer.model_validate(value)
    elif type(value) is ScopedAnswer:
        answer = ScopedAnswer.model_validate(value.model_dump(mode="python"))
    else:
        raise ValueError("answer has the wrong concrete type")
    if len(answer.citations) > 16:
        raise ValueError("public answer exceeds the citation limit")
    if answer.assistance is not None and len(answer.assistance.follow_up_queries) > 8:
        raise ValueError("public answer exceeds the follow-up query limit")
    return answer


def _reject_sensitive_text(value: str) -> str:
    if contains_sensitive_memory_text(value):
        raise ValueError("public message contains sensitive material")
    return value


class HealthState(StrEnum):
    OK = "ok"
    NOT_READY = "not_ready"


class HealthResponse(StrictModel):
    status: HealthState
    checked_at: datetime

    _checked_at_is_utc = field_validator("checked_at")(_require_utc)


class CreateSessionRequest(StrictModel):
    label: SessionLabel | None = None


class SessionResponse(StrictModel):
    session_id: OpaqueId
    label: SessionLabel | None = None
    created_at: datetime
    updated_at: datetime

    _timestamps_are_utc = field_validator("created_at", "updated_at")(_require_utc)

    @model_validator(mode="after")
    def validate_timestamps(self) -> SessionResponse:
        if self.updated_at < self.created_at:
            raise ValueError("session update cannot precede creation")
        return self


class SessionListResponse(StrictModel):
    items: tuple[SessionResponse, ...] = Field(max_length=100)
    next_cursor: PageCursor | None = None


class RunSubmitRequest(StrictModel):
    query: QueryText


class RunAcceptedResponse(StrictModel):
    session_id: OpaqueId
    run_id: OpaqueId
    state: Literal[RunState.QUEUED] = RunState.QUEUED
    created_at: datetime

    _created_at_is_utc = field_validator("created_at")(_require_utc)


class RunFailureCode(StrEnum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_EVIDENCE = "no_evidence"
    SEARCH_FAILED = "search_failed"
    VALIDATION_FAILED = "validation_failed"
    EXECUTION_FAILED = "execution_failed"
    EXPIRED = "expired"


class RunFailure(StrictModel):
    code: RunFailureCode
    message: SafeMessage
    retryable: bool

    _message_is_public = field_validator("message")(_reject_sensitive_text)


class RunStatusResponse(StrictModel):
    session_id: OpaqueId
    run_id: OpaqueId
    state: RunState
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None = None
    cancellation_requested: bool = False
    answer: ScopedAnswer | None = None
    failure: RunFailure | None = None

    _required_timestamps_are_utc = field_validator("created_at", "updated_at")(
        _require_utc
    )
    _terminal_at_is_utc = field_validator("terminal_at")(_require_optional_utc)

    @field_validator("answer", mode="before")
    @classmethod
    def revalidate_answer(cls, value: object) -> ScopedAnswer | None:
        return None if value is None else _revalidate_answer(value)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> RunStatusResponse:
        if self.updated_at < self.created_at:
            raise ValueError("run update cannot precede creation")
        terminal = self.state in TERMINAL_RUN_STATES
        if terminal != (self.terminal_at is not None):
            raise ValueError("terminal timestamp must match terminal state")
        if self.terminal_at is not None and not (
            self.created_at <= self.terminal_at <= self.updated_at
        ):
            raise ValueError("terminal timestamp must be within the run lifetime")
        if self.state is RunState.CANCELLED and not self.cancellation_requested:
            raise ValueError("cancelled runs require a cancellation request")
        if self.cancellation_requested and self.state not in {
            RunState.RUNNING,
            RunState.WAITING_FOR_TOOL,
            RunState.CANCELLED,
        }:
            raise ValueError("cancellation request does not match run state")
        if self.state is RunState.COMPLETED:
            if self.answer is None or self.failure is not None:
                raise ValueError("completed runs require only a public answer")
        elif self.state in {RunState.FAILED, RunState.EXPIRED}:
            if self.failure is None or self.answer is not None:
                raise ValueError("failed or expired runs require only a safe failure")
            if (self.state is RunState.EXPIRED) != (
                self.failure.code is RunFailureCode.EXPIRED
            ):
                raise ValueError("expired failure code must match run state")
        elif self.answer is not None or self.failure is not None:
            raise ValueError("other run states cannot expose answer or failure")
        return self


class CancellationResponse(StrictModel):
    run_id: OpaqueId
    state: RunState
    cancellation_requested: bool
    changed: bool
    requested_at: datetime | None = None

    _requested_at_is_utc = field_validator("requested_at")(_require_optional_utc)

    @model_validator(mode="after")
    def validate_result(self) -> CancellationResponse:
        if self.changed and not self.cancellation_requested:
            raise ValueError("changed cancellation must be requested")
        if self.cancellation_requested != (self.requested_at is not None):
            raise ValueError("requested cancellation requires its public timestamp")
        if self.state is RunState.CANCELLED and not self.cancellation_requested:
            raise ValueError("cancelled runs require a cancellation request")
        if self.cancellation_requested and self.state not in {
            RunState.RUNNING,
            RunState.WAITING_FOR_TOOL,
            RunState.CANCELLED,
        }:
            raise ValueError("cancellation request does not match run state")
        return self


class DeletionResponse(StrictModel):
    deleted_count: int = Field(ge=0, le=_MAX_EVENT_SEQUENCE)
    completed_at: datetime

    _completed_at_is_utc = field_validator("completed_at")(_require_utc)


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal_error"


class FieldIssue(StrictModel):
    field: FieldPath
    message: SafeMessage

    _message_is_public = field_validator("message")(_reject_sensitive_text)


class ErrorDetail(StrictModel):
    code: ErrorCode
    message: SafeMessage
    correlation_id: OpaqueId
    retryable: bool
    field_issues: tuple[FieldIssue, ...] = Field(default=(), max_length=16)

    _message_is_public = field_validator("message")(_reject_sensitive_text)


class ErrorEnvelope(StrictModel):
    error: ErrorDetail


class RunEventType(StrEnum):
    STATUS = "run.status"
    COMPLETED = "run.completed"
    FAILED = "run.failed"
    CANCELLED = "run.cancelled"
    EXPIRED = "run.expired"


_TERMINAL_EVENT_STATE = {
    RunEventType.COMPLETED: RunState.COMPLETED,
    RunEventType.FAILED: RunState.FAILED,
    RunEventType.CANCELLED: RunState.CANCELLED,
    RunEventType.EXPIRED: RunState.EXPIRED,
}


class RunEvent(StrictModel):
    schema_version: Literal[1] = 1
    sequence: int = Field(ge=1, le=_MAX_EVENT_SEQUENCE)
    run_id: OpaqueId
    event_type: RunEventType
    state: RunState
    occurred_at: datetime
    message: SafeMessage
    answer: ScopedAnswer | None = None
    failure: RunFailure | None = None

    _occurred_at_is_utc = field_validator("occurred_at")(_require_utc)
    _message_is_public = field_validator("message")(_reject_sensitive_text)

    @field_validator("answer", mode="before")
    @classmethod
    def revalidate_answer(cls, value: object) -> ScopedAnswer | None:
        return None if value is None else _revalidate_answer(value)

    @model_validator(mode="after")
    def validate_event(self) -> RunEvent:
        terminal_state = _TERMINAL_EVENT_STATE.get(self.event_type)
        if self.event_type is RunEventType.STATUS:
            if self.state in TERMINAL_RUN_STATES:
                raise ValueError("status events cannot represent terminal states")
        elif self.state is not terminal_state:
            raise ValueError("terminal event type must match run state")
        if self.state is RunState.COMPLETED:
            if self.answer is None or self.failure is not None:
                raise ValueError("completed events require only a public answer")
        elif self.state in {RunState.FAILED, RunState.EXPIRED}:
            if self.failure is None or self.answer is not None:
                raise ValueError("failed or expired events require only a safe failure")
            if (self.state is RunState.EXPIRED) != (
                self.failure.code is RunFailureCode.EXPIRED
            ):
                raise ValueError("expired failure code must match event state")
        elif self.answer is not None or self.failure is not None:
            raise ValueError("other events cannot expose answer or failure")
        return self


def encode_sse(event: RunEvent) -> bytes:
    """Encode one validated event for a Starlette ``StreamingResponse`` body."""

    if type(event) is not RunEvent:
        raise ValueError("SSE event has the wrong concrete type")
    checked = RunEvent.model_validate(event.model_dump(mode="python"))
    data = checked.model_dump_json(exclude_none=True)
    return (
        f"id: {checked.sequence}\nevent: {checked.event_type.value}\ndata: {data}\n\n"
    ).encode()


def parse_last_event_id(value: str | None) -> int | None:
    """Parse a resumable SSE cursor without accepting signs, whitespace, or zero."""

    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("invalid Last-Event-ID")
    try:
        checked = _LAST_EVENT_ID.validate_python(value, strict=True)
    except ValidationError as exc:
        raise ValueError("invalid Last-Event-ID") from exc
    sequence = int(checked)
    if sequence > _MAX_EVENT_SEQUENCE:
        raise ValueError("invalid Last-Event-ID")
    return sequence


SSE_HEARTBEAT = b": heartbeat\n\n"


__all__ = [
    "SSE_HEARTBEAT",
    "CancellationResponse",
    "CreateSessionRequest",
    "DeletionResponse",
    "ErrorCode",
    "ErrorDetail",
    "ErrorEnvelope",
    "FieldIssue",
    "HealthResponse",
    "HealthState",
    "LastEventId",
    "PageCursor",
    "RunAcceptedResponse",
    "RunEvent",
    "RunEventType",
    "RunFailure",
    "RunFailureCode",
    "RunStatusResponse",
    "RunSubmitRequest",
    "SessionListResponse",
    "SessionResponse",
    "encode_sse",
    "parse_last_event_id",
]
