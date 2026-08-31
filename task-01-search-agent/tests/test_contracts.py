from __future__ import annotations

import json

import pytest
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from search_agent import (
    Citation,
    EventType,
    FailureReason,
    OptionalAssistance,
    PublicEvent,
    QueryPlan,
    ScopedAnswer,
    SearchQuery,
    TerminalState,
    ToolBudget,
)
from search_agent.state import RunSnapshot, RunStatus

URL_ADAPTER = TypeAdapter(AnyHttpUrl)


def _url(value: str) -> AnyHttpUrl:
    return URL_ADAPTER.validate_python(value)


def test_query_plan_rejects_over_budget_search_count() -> None:
    with pytest.raises(ValidationError, match="query budget"):
        QueryPlan(
            tool_budget=ToolBudget(max_search_queries=1, max_fetches=4),
            searches=(
                SearchQuery(text="siemens sustainability report", max_results=2),
                SearchQuery(text="siemens decarbonization targets", max_results=2),
            ),
        )


def test_query_plan_rejects_over_budget_fetches() -> None:
    with pytest.raises(ValidationError, match="fetch budget"):
        QueryPlan(
            tool_budget=ToolBudget(max_search_queries=2, max_fetches=3),
            searches=(
                SearchQuery(text="siemens sustainability report", max_results=2),
                SearchQuery(text="siemens decarbonization targets", max_results=2),
            ),
        )


def test_scoped_answer_requires_one_source_url_per_evidence_id() -> None:
    with pytest.raises(ValidationError, match="one source URL"):
        ScopedAnswer(
            answer_text="Evidence-backed answer",
            citations=(
                Citation(
                    claim="Claim A",
                    evidence_id="ev-report",
                    source_url=_url("https://example.com/report"),
                ),
                Citation(
                    claim="Claim B",
                    evidence_id="ev-report",
                    source_url=_url("https://example.com/report-2"),
                ),
            ),
        )


def test_scoped_answer_rejects_a_repeated_claim_for_one_evidence_id() -> None:
    citation = Citation(
        claim="Claim A",
        evidence_id="ev-report",
        source_url=_url("https://example.com/report"),
    )

    with pytest.raises(ValidationError, match="unique per evidence id"):
        ScopedAnswer(answer_text="Claim A Claim A", citations=(citation, citation))


def test_scoped_answer_allows_distinct_claims_from_one_source() -> None:
    # A single page routinely supports more than one sentence of an answer.
    answer = ScopedAnswer(
        answer_text="Claim A Claim B",
        citations=(
            Citation(
                claim="Claim A",
                evidence_id="ev-report",
                source_url=_url("https://example.com/report"),
            ),
            Citation(
                claim="Claim B",
                evidence_id="ev-report",
                source_url=_url("https://example.com/report"),
            ),
        ),
    )

    assert len(answer.citations) == 2


def test_scoped_answer_allows_a_direct_answer_without_citations() -> None:
    answer = ScopedAnswer(answer_text="Please clarify the request.", citations=())

    assert answer.citations == ()


def test_public_event_failure_reason_only_exists_for_failed_runs() -> None:
    with pytest.raises(ValidationError, match="only public for failed runs"):
        PublicEvent(
            tenant_id="tenant-123",
            session_id="session-123",
            run_id="run-123",
            event_type=EventType.RUN_COMPLETED,
            message="Completed cited answer",
            terminal_state=TerminalState.COMPLETED,
            failure_reason=FailureReason.NO_EVIDENCE,
        )


def test_public_event_rejects_mismatched_terminal_state() -> None:
    with pytest.raises(ValidationError, match="public terminal event"):
        PublicEvent(
            tenant_id="tenant-123",
            session_id="session-123",
            run_id="run-123",
            event_type=EventType.RUN_CANCELLED,
            message="Cancelled cited answer",
            terminal_state=TerminalState.FAILED,
        )


def test_models_are_strict_about_scalar_types() -> None:
    with pytest.raises(ValidationError):
        ToolBudget(max_search_queries="1", max_fetches=2)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SearchQuery(text=123, max_results=2)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PublicEvent(
            tenant_id="tenant-123",
            session_id="session-123",
            run_id=123,  # type: ignore[arg-type]
            event_type=EventType.RUN_CREATED,
            message="Created run",
        )


def test_run_snapshot_schema_stays_public_and_reasoning_free() -> None:
    schema = RunSnapshot.model_json_schema()
    assert set(schema["properties"]) == {
        "tenant_id",
        "session_id",
        "run_id",
        "status",
        "request",
        "plan",
        "hits",
        "evidence",
        "answer",
        "terminal_state",
        "failure_reason",
    }
    assert schema["properties"]["status"]["$ref"] == "#/$defs/RunStatus"
    assert schema["properties"]["plan"]["anyOf"][0]["$ref"] == "#/$defs/QueryPlan"
    assert "reasoning" not in json.dumps(schema)


def test_scoped_answer_allows_optional_assistance_without_extra_fields() -> None:
    answer = ScopedAnswer(
        answer_text="Siemens links decarbonization targets to product and operations work.",
        citations=(
            Citation(
                claim="Siemens links decarbonization targets to product and operations work.",
                evidence_id="ev-siemens-report",
                source_url=_url("https://example.com/report"),
            ),
        ),
        assistance=OptionalAssistance(
            offer="I can compare this report with the previous year if helpful.",
            follow_up_queries=("compare siemens sustainability reports 2025 2026",),
        ),
    )

    assert answer.assistance is not None
    assert answer.assistance.follow_up_queries == (
        "compare siemens sustainability reports 2025 2026",
    )


def test_terminal_runs_require_matching_terminal_fields() -> None:
    with pytest.raises(ValidationError, match="terminal_state must match"):
        RunSnapshot(
            tenant_id="tenant-123",
            session_id="session-123",
            run_id="run-123",
            status=RunStatus.COMPLETED,
            request="Find the latest Siemens sustainability report",
            terminal_state=TerminalState.FAILED,
            answer=ScopedAnswer(
                answer_text="Answer",
                citations=(
                    Citation(
                        claim="Answer",
                        evidence_id="ev-answer",
                        source_url=_url("https://example.com/answer"),
                    ),
                ),
            ),
        )
