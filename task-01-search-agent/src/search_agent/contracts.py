"""Strict public contracts for the bounded research agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

OpaqueId = Annotated[
    str,
    StringConstraints(
        min_length=6,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$",
    ),
]
QueryText = Annotated[
    str, StringConstraints(min_length=3, max_length=240, strip_whitespace=True)
]
AnswerText = Annotated[
    str, StringConstraints(min_length=1, max_length=4000, strip_whitespace=True)
]
ConversationAnswerText = Annotated[
    str, StringConstraints(min_length=1, max_length=2000, strip_whitespace=True)
]
SourceText = Annotated[
    str, StringConstraints(min_length=1, max_length=400, strip_whitespace=True)
]
EvidenceId = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=32,
        pattern=r"^ev-[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
TraceName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    ),
]
TraceLabel = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=40,
        pattern=r"^[A-Za-z0-9]+(?:[._:/-][A-Za-z0-9]+)*$",
    ),
]
TraceSafeId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9]+(?:[._:-][A-Za-z0-9]+)*$",
    ),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FailureReason(StrEnum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    NO_EVIDENCE = "no_evidence"
    SEARCH_FAILED = "search_failed"
    VALIDATION_FAILED = "validation_failed"


class TerminalState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TraceStage(StrEnum):
    RUN = "run"
    PLAN = "plan"
    SEARCH = "search"
    FETCH = "fetch"
    EXTRACT = "extract"
    RANK = "rank"
    SYNTHESIZE = "synthesize"
    VALIDATE = "validate"
    FINAL = "final"
    TRACE = "trace"


class TraceOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    TRUNCATED = "truncated"


class EventType(StrEnum):
    RUN_CREATED = "run_created"
    PLAN_ACCEPTED = "plan_accepted"
    SEARCH_STARTED = "search_started"
    EVIDENCE_READY = "evidence_ready"
    ANSWER_DRAFTED = "answer_drafted"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


class ToolBudget(StrictModel):
    max_search_queries: int = Field(ge=0, le=8)
    max_fetches: int = Field(ge=0, le=24)


class SearchQuery(StrictModel):
    text: QueryText
    max_results: int = Field(ge=1, le=5)


class QueryPlan(StrictModel):
    tool_budget: ToolBudget
    searches: tuple[SearchQuery, ...]

    @model_validator(mode="after")
    def validate_budget(self) -> QueryPlan:
        if len(self.searches) > self.tool_budget.max_search_queries:
            msg = "search plan exceeds query budget"
            raise ValueError(msg)
        if (
            sum(query.max_results for query in self.searches)
            > self.tool_budget.max_fetches
        ):
            msg = "search plan exceeds fetch budget"
            raise ValueError(msg)
        return self


class ConversationTurn(StrictModel):
    """One prior completed public turn; identity and internal state stay outside."""

    request: QueryText
    answer: ConversationAnswerText


class ActionTraceRecord(StrictModel):
    """Privacy-safe action metadata; raw user/model text and URLs are excluded."""

    stage: TraceStage
    action: TraceName
    outcome: TraceOutcome
    reason: TraceName | None = None
    provider: TraceLabel | None = None
    format: TraceLabel | None = None
    profile: TraceLabel | None = None
    safe_id: TraceSafeId | None = None
    context_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    count: int | None = Field(default=None, ge=0, le=1_000_000)
    bytes_count: int | None = Field(default=None, ge=0, le=128 * 1024 * 1024)
    duration_ms: int | None = Field(default=None, ge=0, le=600_000)


class ResearchTraceSink(Protocol):
    def record(self, record: ActionTraceRecord) -> None: ...


def validate_conversation_context(value: object) -> tuple[ConversationTurn, ...]:
    """Strictly revalidate a small, public, same-session conversation window."""

    if type(value) is not tuple or len(value) > 6:
        raise ValueError("conversation context must contain at most six turns")
    checked: list[ConversationTurn] = []
    total_characters = 0
    for item in value:
        if type(item) is not ConversationTurn:
            raise ValueError("conversation context contains an invalid turn")
        turn = ConversationTurn.model_validate(
            item.model_dump(mode="python", warnings="error"), strict=True
        )
        if turn != item:
            raise ValueError("conversation context changed during validation")
        total_characters += len(turn.request) + len(turn.answer)
        if total_characters > 12_000:
            raise ValueError("conversation context exceeds its size bound")
        checked.append(turn)
    return tuple(checked)


class SearchHit(StrictModel):
    title: SourceText
    url: AnyHttpUrl
    snippet: SourceText
    rank: int = Field(ge=1, le=20)


class ExtractedEvidence(StrictModel):
    evidence_id: EvidenceId
    source_url: AnyHttpUrl
    source_title: SourceText
    summary: SourceText
    quotes: tuple[SourceText, ...] = ()


class Citation(StrictModel):
    claim: SourceText
    evidence_id: EvidenceId
    source_url: AnyHttpUrl


class OptionalAssistance(StrictModel):
    offer: SourceText
    follow_up_queries: tuple[QueryText, ...] = ()


class ScopedAnswer(StrictModel):
    answer_text: AnswerText
    citations: tuple[Citation, ...]
    assistance: OptionalAssistance | None = None

    @model_validator(mode="after")
    def require_unique_citations(self) -> ScopedAnswer:
        citation_ids = {citation.evidence_id for citation in self.citations}
        if len(citation_ids) != len(self.citations):
            msg = "citation evidence ids must be unique"
            raise ValueError(msg)
        return self


class PublicEvent(StrictModel):
    tenant_id: OpaqueId
    session_id: OpaqueId
    run_id: OpaqueId
    event_type: EventType
    message: SourceText
    terminal_state: TerminalState | None = None
    failure_reason: FailureReason | None = None

    @model_validator(mode="after")
    def validate_terminal_fields(self) -> PublicEvent:
        expected_terminal = {
            EventType.RUN_COMPLETED: TerminalState.COMPLETED,
            EventType.RUN_FAILED: TerminalState.FAILED,
            EventType.RUN_CANCELLED: TerminalState.CANCELLED,
        }.get(self.event_type)
        if (expected_terminal is not None) != (self.terminal_state is not None):
            msg = "terminal_state must match terminal events"
            raise ValueError(msg)
        if (
            expected_terminal is not None
            and self.terminal_state is not expected_terminal
        ):
            msg = "terminal_state must match the public terminal event"
            raise ValueError(msg)
        if self.event_type == EventType.RUN_FAILED and self.failure_reason is None:
            msg = "run_failed events require failure_reason"
            raise ValueError(msg)
        if self.event_type != EventType.RUN_FAILED and self.failure_reason is not None:
            msg = "failure_reason is only public for failed runs"
            raise ValueError(msg)
        return self
