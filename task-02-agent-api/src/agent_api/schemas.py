"""Strict public HTTP and SSE schemas for the versioned Agent API."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

from pydantic import (
    AfterValidator,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from search_agent.contracts import OpaqueId, QueryText, ScopedAnswer, StrictModel
from search_agent.memory.contracts import contains_sensitive_memory_text

from .ports import TERMINAL_RUN_STATES, RunFailureCode, RunState

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

_MAX_EVENT_SEQUENCE = 9_223_372_036_854_775_807
_PRIVATE_FIELD_TOKENS = frozenset(
    {
        "authorization",
        "chain",
        "credential",
        "exception",
        "internal",
        "password",
        "prompt",
        "raw",
        "reasoning",
        "secret",
        "stack",
        "tenant",
        "token",
        "traceback",
    }
)
_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "apikey",
        "accesstoken",
        "auth",
        "authorization",
        "clientassertion",
        "clientsecret",
        "credential",
        "credentials",
        "idtoken",
        "key",
        "passwd",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "sessiontoken",
        "sig",
        "signature",
        "token",
        "xamzcredential",
        "xamzsecuritytoken",
        "xamzsignature",
        "xgoogcredential",
        "xgoogsignature",
    }
)
_PUBLIC_MESSAGE_PATTERNS = (
    re.compile(r"(?i)\btraceback\b"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^@\s/]+@"),
    # URL paths and nested query values are not parsed as top-level fields. Match a
    # sensitive label plus an assignment delimiter after decoding all separators.
    re.compile(
        r"(?i)(?<![a-z0-9])(?:"
        r"api[\s._-]*key|access[\s._-]*token|auth(?:orization)?|"
        r"client[\s._-]*(?:assertion|secret)|credentials?|"
        r"id[\s._-]*token|pass(?:word|wd)|private[\s._-]*key|"
        r"refresh[\s._-]*token|secret|session[\s._-]*token|"
        r"sig(?:nature)?|token|x[\s._-]*amz[\s._-]*(?:credential|security[\s._-]*token|signature)|"
        r"x[\s._-]*goog[\s._-]*(?:credential|signature)"
        r")\s*(?:=|:)(?=.)"
    ),
)


def _bounded_event_id(value: str) -> str:
    if int(value) > _MAX_EVENT_SEQUENCE:
        raise ValueError("event sequence exceeds the public bound")
    return value


LastEventId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=19, pattern=r"^[1-9][0-9]{0,18}$"),
    AfterValidator(_bounded_event_id),
]
_LAST_EVENT_ID = TypeAdapter(LastEventId)


def _require_utc(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise ValueError("timestamp must be an exact datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def _require_optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _require_utc(value)


def validate_public_answer(value: object) -> ScopedAnswer:
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
    _reject_sensitive_text(answer.answer_text)
    for citation in answer.citations:
        _reject_sensitive_text(citation.claim)
        _require_public_source_url(str(citation.source_url))
    if answer.assistance is not None:
        _reject_sensitive_text(answer.assistance.offer)
        for query in answer.assistance.follow_up_queries:
            _reject_sensitive_text(query)
    return answer


def _reject_sensitive_text(value: str) -> str:
    if contains_sensitive_memory_text(value) or any(
        pattern.search(value) for pattern in _PUBLIC_MESSAGE_PATTERNS
    ):
        raise ValueError("public message contains sensitive material")
    return value


def _require_public_source_url(value: str) -> None:
    """Reject citation URLs that reveal credentials or non-public infrastructure."""

    parsed = urlsplit(value)
    host = parsed.hostname
    if parsed.username is not None or parsed.password is not None or host is None:
        raise ValueError("citation URL is not safe to expose")
    normalized_host = host.rstrip(".").lower()
    if contains_sensitive_memory_text(normalized_host):
        raise ValueError("citation URL contains sensitive material")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if normalized_host == "localhost" or normalized_host.endswith(
            (".localhost", ".local", ".internal")
        ):
            raise ValueError("citation URL is not public") from None
    else:
        if not address.is_global:
            raise ValueError("citation URL is not public")
    if parsed.fragment:
        raise ValueError("citation URL fragments are not public")
    _validated_public_url_component(parsed.path, plus=False)
    _validated_public_url_component(parsed.query, plus=True)
    for name, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        decoded_name = _validated_public_url_component(name, plus=True)
        normalized_name = re.sub(r"[^a-z0-9]", "", decoded_name.lower())
        if normalized_name in _SENSITIVE_QUERY_NAMES:
            raise ValueError("citation URL contains a sensitive query field")
        _validated_public_url_component(query_value, plus=True)


def _validated_public_url_component(value: str, *, plus: bool) -> str:
    try:
        decoded = _fully_decode_url_component(value, plus=plus)
        _reject_sensitive_text(decoded)
    except ValueError:
        raise ValueError("citation URL contains sensitive material") from None
    return decoded


def _fully_decode_url_component(value: str, *, plus: bool) -> str:
    """Expose nested percent encoding before applying the disclosure boundary."""

    decode = unquote_plus if plus else unquote
    decoded = value
    # Every effective decoding pass shortens a percent-encoded component. The input
    # length is therefore a strict termination bound even for nested encodings.
    try:
        for _ in range(len(value) + 1):
            candidate = decode(decoded, errors="strict")
            if candidate == decoded:
                return decoded
            decoded = candidate
    except UnicodeDecodeError:
        raise ValueError("citation URL encoding is not public") from None
    raise ValueError("citation URL encoding exceeds the public bound")


def _reject_private_field_path(value: str) -> str:
    tokens = {
        token
        for component in value.split(".")
        for token in component.lower().split("_")
    }
    if tokens & _PRIVATE_FIELD_TOKENS:
        raise ValueError("field path identifies a private diagnostic channel")
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

    @field_validator("items", mode="before")
    @classmethod
    def revalidate_items(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        return tuple(
            SessionResponse.model_validate(
                item.model_dump(mode="python", warnings=False)
                if type(item) is SessionResponse
                else item
            )
            for item in value
        )


class RunSubmitRequest(StrictModel):
    query: QueryText


class RunAcceptedResponse(StrictModel):
    session_id: OpaqueId
    run_id: OpaqueId
    state: Literal[RunState.QUEUED] = RunState.QUEUED
    created_at: datetime

    _created_at_is_utc = field_validator("created_at")(_require_utc)


class RunFailure(StrictModel):
    code: RunFailureCode
    message: SafeMessage
    retryable: bool

    _message_is_public = field_validator("message")(_reject_sensitive_text)


_PUBLIC_RUN_FAILURES: dict[RunFailureCode, tuple[str, bool]] = {
    RunFailureCode.BUDGET_EXHAUSTED: ("Run exhausted its configured budget.", False),
    RunFailureCode.NO_EVIDENCE: ("No sufficient public evidence was found.", False),
    RunFailureCode.SEARCH_FAILED: ("Public evidence search failed.", True),
    RunFailureCode.VALIDATION_FAILED: ("Run output failed validation.", False),
    RunFailureCode.EXECUTION_FAILED: ("Run execution failed.", True),
    RunFailureCode.EXPIRED: ("Run expired before completion.", True),
}


def public_run_failure(code: RunFailureCode) -> RunFailure:
    message, retryable = _PUBLIC_RUN_FAILURES[code]
    return RunFailure(code=code, message=message, retryable=retryable)


def _revalidate_failure(value: object) -> RunFailure:
    if isinstance(value, dict):
        return RunFailure.model_validate(value)
    if type(value) is not RunFailure:
        raise ValueError("failure has the wrong concrete type")
    return RunFailure.model_validate(value.model_dump(mode="python", warnings=False))


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
        return None if value is None else validate_public_answer(value)

    @field_validator("failure", mode="before")
    @classmethod
    def revalidate_failure(cls, value: object) -> RunFailure | None:
        return None if value is None else _revalidate_failure(value)

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
    _field_is_public = field_validator("field")(_reject_private_field_path)


def _revalidate_field_issue(value: object) -> FieldIssue:
    if isinstance(value, dict):
        return FieldIssue.model_validate(value)
    if type(value) is not FieldIssue:
        raise ValueError("field issue has the wrong concrete type")
    return FieldIssue.model_validate(value.model_dump(mode="python", warnings=False))


class ErrorDetail(StrictModel):
    code: ErrorCode
    message: SafeMessage
    correlation_id: OpaqueId
    retryable: bool
    field_issues: tuple[FieldIssue, ...] = Field(default=(), max_length=16)

    _message_is_public = field_validator("message")(_reject_sensitive_text)

    @field_validator("field_issues", mode="before")
    @classmethod
    def revalidate_field_issues(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        return tuple(_revalidate_field_issue(item) for item in value)


def _revalidate_error_detail(value: object) -> ErrorDetail:
    if isinstance(value, dict):
        return ErrorDetail.model_validate(value)
    if type(value) is not ErrorDetail:
        raise ValueError("error detail has the wrong concrete type")
    return ErrorDetail.model_validate(value.model_dump(mode="python", warnings=False))


class ErrorEnvelope(StrictModel):
    error: ErrorDetail

    @field_validator("error", mode="before")
    @classmethod
    def revalidate_error(cls, value: object) -> ErrorDetail:
        return _revalidate_error_detail(value)


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
        return None if value is None else validate_public_answer(value)

    @field_validator("failure", mode="before")
    @classmethod
    def revalidate_failure(cls, value: object) -> RunFailure | None:
        return None if value is None else _revalidate_failure(value)

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
    "public_run_failure",
]
