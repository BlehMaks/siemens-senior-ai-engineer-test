"""Strict public contracts for the bounded research agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

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
    def require_citations(self) -> ScopedAnswer:
        if not self.citations:
            msg = "scoped answers require at least one citation"
            raise ValueError(msg)
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
        terminal_event = self.event_type in {
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
        }
        if terminal_event != (self.terminal_state is not None):
            msg = "terminal_state must match terminal events"
            raise ValueError(msg)
        if self.event_type == EventType.RUN_FAILED and self.failure_reason is None:
            msg = "run_failed events require failure_reason"
            raise ValueError(msg)
        if self.event_type != EventType.RUN_FAILED and self.failure_reason is not None:
            msg = "failure_reason is only public for failed runs"
            raise ValueError(msg)
        return self
