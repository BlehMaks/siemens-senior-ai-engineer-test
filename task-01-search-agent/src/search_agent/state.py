"""Legal run states and transition helpers for the bounded search agent."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import Field, model_validator

from .contracts import (
    EventType,
    ExtractedEvidence,
    FailureReason,
    OpaqueId,
    PublicEvent,
    QueryPlan,
    ScopedAnswer,
    SearchHit,
    StrictModel,
    TerminalState,
)


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNED = "planned"
    SEARCHING = "searching"
    EVIDENCE_READY = "evidence_ready"
    ANSWER_READY = "answer_ready"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunSnapshot(StrictModel):
    tenant_id: OpaqueId
    session_id: OpaqueId
    run_id: OpaqueId
    status: RunStatus
    request: str = Field(min_length=1, max_length=400)
    plan: QueryPlan | None = None
    hits: tuple[SearchHit, ...] = ()
    evidence: tuple[ExtractedEvidence, ...] = ()
    answer: ScopedAnswer | None = None
    terminal_state: TerminalState | None = None
    failure_reason: FailureReason | None = None

    @model_validator(mode="after")
    def validate_state_invariants(self) -> RunSnapshot:
        if self.status is RunStatus.PLANNED and self.plan is None:
            msg = "planned runs require a query plan"
            raise ValueError(msg)
        if self.status is RunStatus.SEARCHING and self.plan is None:
            msg = "searching runs require a query plan"
            raise ValueError(msg)
        if self.status is RunStatus.EVIDENCE_READY and not self.evidence:
            msg = "evidence_ready runs require evidence"
            raise ValueError(msg)
        if self.status is RunStatus.ANSWER_READY and self.answer is None:
            msg = "answer_ready runs require an answer"
            raise ValueError(msg)
        if self.status is RunStatus.ANSWER_READY and (
            self.answer is not None
            and bool(self.answer.citations) != bool(self.evidence)
        ):
            msg = "answer citations must match the evidence-backed run path"
            raise ValueError(msg)
        terminal_statuses = {
            RunStatus.COMPLETED: TerminalState.COMPLETED,
            RunStatus.FAILED: TerminalState.FAILED,
            RunStatus.CANCELLED: TerminalState.CANCELLED,
        }
        expected_terminal = terminal_statuses.get(self.status)
        if expected_terminal is None:
            if self.terminal_state is not None or self.failure_reason is not None:
                msg = "non-terminal runs cannot expose terminal fields"
                raise ValueError(msg)
            return self
        if self.terminal_state is not expected_terminal:
            msg = "terminal_state must match terminal run status"
            raise ValueError(msg)
        if self.status is RunStatus.COMPLETED and self.answer is None:
            msg = "completed runs require an answer"
            raise ValueError(msg)
        if self.status is RunStatus.COMPLETED and (
            self.answer is not None
            and bool(self.answer.citations) != bool(self.evidence)
        ):
            msg = "answer citations must match the evidence-backed run path"
            raise ValueError(msg)
        if self.status is RunStatus.FAILED and self.failure_reason is None:
            msg = "failed runs require failure_reason"
            raise ValueError(msg)
        if self.status is not RunStatus.FAILED and self.failure_reason is not None:
            msg = "only failed runs expose failure_reason"
            raise ValueError(msg)
        return self


class IllegalTransitionError(ValueError):
    """Raised when callers attempt a state transition outside the legal graph."""


class RunStateGraph:
    _ALLOWED: ClassVar[dict[RunStatus, tuple[RunStatus, ...]]] = {
        RunStatus.CREATED: (
            RunStatus.PLANNED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ),
        RunStatus.PLANNED: (
            RunStatus.SEARCHING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ),
        RunStatus.SEARCHING: (
            RunStatus.EVIDENCE_READY,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ),
        RunStatus.EVIDENCE_READY: (
            RunStatus.ANSWER_READY,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ),
        RunStatus.ANSWER_READY: (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ),
        RunStatus.COMPLETED: (),
        RunStatus.FAILED: (),
        RunStatus.CANCELLED: (),
    }

    @classmethod
    def create(
        cls, tenant_id: OpaqueId, session_id: OpaqueId, run_id: OpaqueId, request: str
    ) -> RunSnapshot:
        return RunSnapshot(
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
            status=RunStatus.CREATED,
            request=request,
        )

    @classmethod
    def accept_plan(
        cls, run: RunSnapshot, plan: QueryPlan
    ) -> tuple[RunSnapshot, PublicEvent]:
        return cls._transition(
            run,
            next_status=RunStatus.PLANNED,
            event_type=EventType.PLAN_ACCEPTED,
            message="Accepted bounded search plan",
            plan=plan,
        )

    @classmethod
    def start_search(cls, run: RunSnapshot) -> tuple[RunSnapshot, PublicEvent]:
        return cls._transition(
            run,
            next_status=RunStatus.SEARCHING,
            event_type=EventType.SEARCH_STARTED,
            message="Started bounded search",
        )

    @classmethod
    def record_evidence(
        cls,
        run: RunSnapshot,
        *,
        hits: tuple[SearchHit, ...],
        evidence: tuple[ExtractedEvidence, ...],
    ) -> tuple[RunSnapshot, PublicEvent]:
        return cls._transition(
            run,
            next_status=RunStatus.EVIDENCE_READY,
            event_type=EventType.EVIDENCE_READY,
            message="Collected evidence for answer synthesis",
            hits=hits,
            evidence=evidence,
        )

    @classmethod
    def draft_answer(
        cls,
        run: RunSnapshot,
        answer: ScopedAnswer,
    ) -> tuple[RunSnapshot, PublicEvent]:
        if not answer.citations:
            raise ValueError("cited answers require at least one citation")
        return cls._transition(
            run,
            next_status=RunStatus.ANSWER_READY,
            event_type=EventType.ANSWER_DRAFTED,
            message="Drafted cited answer",
            answer=answer,
        )

    @classmethod
    def draft_direct_answer(
        cls,
        run: RunSnapshot,
        answer: ScopedAnswer,
    ) -> tuple[RunSnapshot, PublicEvent]:
        """Record a validated answer that deliberately skipped web search."""

        if answer.citations:
            raise ValueError("direct answers cannot contain citations")
        if run.status is not RunStatus.CREATED:
            msg = f"illegal transition: {run.status.value} -> answer_ready"
            raise IllegalTransitionError(msg)
        return cls._transition(
            run,
            next_status=RunStatus.ANSWER_READY,
            event_type=EventType.ANSWER_DRAFTED,
            message="Drafted answer without web search",
            answer=answer,
            allow_created_answer=True,
        )

    @classmethod
    def complete(cls, run: RunSnapshot) -> tuple[RunSnapshot, PublicEvent]:
        return cls._transition(
            run,
            next_status=RunStatus.COMPLETED,
            event_type=EventType.RUN_COMPLETED,
            message=(
                "Completed cited answer"
                if run.answer is not None and run.answer.citations
                else "Completed answer without web search"
            ),
            terminal_state=TerminalState.COMPLETED,
        )

    @classmethod
    def fail(
        cls,
        run: RunSnapshot,
        reason: FailureReason,
        *,
        message: str = "Run failed within policy bounds",
    ) -> tuple[RunSnapshot, PublicEvent]:
        return cls._transition(
            run,
            next_status=RunStatus.FAILED,
            event_type=EventType.RUN_FAILED,
            message=message,
            terminal_state=TerminalState.FAILED,
            failure_reason=reason,
        )

    @classmethod
    def cancel(
        cls,
        run: RunSnapshot,
        *,
        message: str = "Run cancelled before completion",
    ) -> tuple[RunSnapshot, PublicEvent]:
        return cls._transition(
            run,
            next_status=RunStatus.CANCELLED,
            event_type=EventType.RUN_CANCELLED,
            message=message,
            terminal_state=TerminalState.CANCELLED,
        )

    @classmethod
    def _transition(
        cls,
        run: RunSnapshot,
        *,
        next_status: RunStatus,
        event_type: EventType,
        message: str,
        plan: QueryPlan | None = None,
        hits: tuple[SearchHit, ...] | None = None,
        evidence: tuple[ExtractedEvidence, ...] | None = None,
        answer: ScopedAnswer | None = None,
        terminal_state: TerminalState | None = None,
        failure_reason: FailureReason | None = None,
        allow_created_answer: bool = False,
    ) -> tuple[RunSnapshot, PublicEvent]:
        created_answer = (
            allow_created_answer
            and run.status is RunStatus.CREATED
            and next_status is RunStatus.ANSWER_READY
        )
        if not created_answer and next_status not in cls._ALLOWED[run.status]:
            msg = f"illegal transition: {run.status.value} -> {next_status.value}"
            raise IllegalTransitionError(msg)
        next_values = run.model_dump(mode="python")
        next_values.update(
            {
                "status": next_status,
                "plan": run.plan if plan is None else plan,
                "hits": run.hits if hits is None else hits,
                "evidence": run.evidence if evidence is None else evidence,
                "answer": run.answer if answer is None else answer,
                "terminal_state": terminal_state,
                "failure_reason": failure_reason,
            }
        )
        # Pydantic model_copy skips validation; every public transition must re-check
        # the destination state's cross-field invariants.
        next_run = RunSnapshot.model_validate(next_values)
        event = PublicEvent(
            tenant_id=run.tenant_id,
            session_id=run.session_id,
            run_id=run.run_id,
            event_type=event_type,
            message=message,
            terminal_state=terminal_state,
            failure_reason=failure_reason,
        )
        return next_run, event
