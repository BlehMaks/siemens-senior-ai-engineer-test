from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from search_agent import (
    Citation,
    ExtractedEvidence,
    FailureReason,
    PublicEvent,
    QueryPlan,
    RunSnapshot,
    ScopedAnswer,
    SearchHit,
    SearchQuery,
    ToolBudget,
)
from search_agent.state import IllegalTransitionError, RunStateGraph, RunStatus

URL_ADAPTER = TypeAdapter(AnyHttpUrl)
TransitionBuilder = Callable[[RunSnapshot], tuple[RunSnapshot, PublicEvent]]


def _url(value: str) -> AnyHttpUrl:
    return URL_ADAPTER.validate_python(value)


def _plan() -> QueryPlan:
    return QueryPlan(
        tool_budget=ToolBudget(max_search_queries=2, max_fetches=4),
        searches=(
            SearchQuery(text="siemens sustainability report", max_results=2),
            SearchQuery(text="siemens decarbonization targets", max_results=2),
        ),
    )


def _evidence() -> tuple[tuple[SearchHit, ...], tuple[ExtractedEvidence, ...]]:
    hits = (
        SearchHit(
            title="Siemens sustainability report",
            url=_url("https://example.com/report"),
            snippet="Annual sustainability overview",
            rank=1,
        ),
    )
    evidence = (
        ExtractedEvidence(
            evidence_id="ev-report",
            source_url=_url("https://example.com/report"),
            source_title="Siemens sustainability report",
            summary="The report covers decarbonization targets.",
            quotes=("Decarbonization targets are tracked annually.",),
        ),
    )
    return hits, evidence


def _answer() -> ScopedAnswer:
    return ScopedAnswer(
        answer_text="Siemens reports annual progress against decarbonization targets.",
        citations=(
            Citation(
                claim="Siemens reports annual progress against decarbonization targets.",
                evidence_id="ev-report",
                source_url=_url("https://example.com/report"),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("builder", "expected_status", "expected_event"),
    [
        (
            lambda run: RunStateGraph.accept_plan(run, _plan()),
            RunStatus.PLANNED,
            "plan_accepted",
        ),
        (
            lambda run: RunStateGraph.start_search(
                RunStateGraph.accept_plan(run, _plan())[0]
            ),
            RunStatus.SEARCHING,
            "search_started",
        ),
        (
            lambda run: RunStateGraph.record_evidence(
                RunStateGraph.start_search(RunStateGraph.accept_plan(run, _plan())[0])[
                    0
                ],
                hits=_evidence()[0],
                evidence=_evidence()[1],
            ),
            RunStatus.EVIDENCE_READY,
            "evidence_ready",
        ),
        (
            lambda run: RunStateGraph.draft_answer(
                RunStateGraph.record_evidence(
                    RunStateGraph.start_search(
                        RunStateGraph.accept_plan(run, _plan())[0]
                    )[0],
                    hits=_evidence()[0],
                    evidence=_evidence()[1],
                )[0],
                _answer(),
            ),
            RunStatus.ANSWER_READY,
            "answer_drafted",
        ),
        (
            lambda run: RunStateGraph.complete(
                RunStateGraph.draft_answer(
                    RunStateGraph.record_evidence(
                        RunStateGraph.start_search(
                            RunStateGraph.accept_plan(run, _plan())[0]
                        )[0],
                        hits=_evidence()[0],
                        evidence=_evidence()[1],
                    )[0],
                    _answer(),
                )[0]
            ),
            RunStatus.COMPLETED,
            "run_completed",
        ),
    ],
)
def test_legal_transitions(
    builder: TransitionBuilder,
    expected_status: RunStatus,
    expected_event: str,
) -> None:
    run = RunStateGraph.create(
        tenant_id="tenant-123",
        session_id="session-123",
        run_id="run-123",
        request="Find the latest Siemens sustainability report",
    )

    next_run, event = builder(run)

    assert next_run.status is expected_status
    assert event.event_type.value == expected_event


@pytest.mark.parametrize(
    ("builder", "match"),
    [
        (lambda run: RunStateGraph.start_search(run), "created -> searching"),
        (lambda run: RunStateGraph.complete(run), "created -> completed"),
    ],
)
def test_illegal_transitions_raise(
    builder: Callable[[RunSnapshot], object],
    match: str,
) -> None:
    run = RunStateGraph.create(
        tenant_id="tenant-123",
        session_id="session-123",
        run_id="run-123",
        request="Find the latest Siemens sustainability report",
    )

    with pytest.raises(IllegalTransitionError, match=match):
        builder(run)


def test_failed_run_emits_terminal_reason() -> None:
    run = RunStateGraph.create(
        tenant_id="tenant-123",
        session_id="session-123",
        run_id="run-123",
        request="Find the latest Siemens sustainability report",
    )
    planned_run, _ = RunStateGraph.accept_plan(run, _plan())
    failed_run, event = RunStateGraph.fail(planned_run, FailureReason.SEARCH_FAILED)

    assert failed_run.status is RunStatus.FAILED
    assert failed_run.failure_reason is FailureReason.SEARCH_FAILED
    assert event.failure_reason is FailureReason.SEARCH_FAILED


def test_terminal_states_do_not_transition_again() -> None:
    run = RunStateGraph.create(
        tenant_id="tenant-123",
        session_id="session-123",
        run_id="run-123",
        request="Find the latest Siemens sustainability report",
    )
    planned_run, _ = RunStateGraph.accept_plan(run, _plan())
    cancelled_run, _ = RunStateGraph.cancel(planned_run)

    with pytest.raises(IllegalTransitionError, match="cancelled -> planned"):
        RunStateGraph.accept_plan(cancelled_run, _plan())


def test_public_evidence_transition_revalidates_destination_invariants() -> None:
    run = RunStateGraph.create(
        tenant_id="tenant-123",
        session_id="session-123",
        run_id="run-123",
        request="Find the latest Siemens sustainability report",
    )
    planned_run, _ = RunStateGraph.accept_plan(run, _plan())
    searching_run, _ = RunStateGraph.start_search(planned_run)

    with pytest.raises(ValidationError, match="evidence_ready runs require evidence"):
        RunStateGraph.record_evidence(searching_run, hits=(), evidence=())
