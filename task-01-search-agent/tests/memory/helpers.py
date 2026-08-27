from __future__ import annotations

from pydantic import AnyHttpUrl

from search_agent import (
    Citation,
    EventType,
    ExtractedEvidence,
    FailureReason,
    PublicEvent,
    QueryPlan,
    RunResult,
    RunSnapshot,
    RunStateGraph,
    RunUsage,
    ScopedAnswer,
    SearchHit,
    SearchQuery,
    ToolBudget,
)
from search_agent.memory import RunReflection, reflect_run

SOURCE_URL = "https://www.siemens.com/reports/sustainability-2025"
CLAIM = "Siemens published its 2025 sustainability report."


def completed_result(
    *,
    tenant_id: str = "tenant-one",
    session_id: str = "session-one",
    run_id: str = "run-000001",
    request: str = "Find the Siemens 2025 sustainability report.",
    failed_pages: int = 0,
    source_url: str = SOURCE_URL,
    evidence_summary: str = CLAIM,
) -> RunResult:
    checked_url = AnyHttpUrl(source_url)
    snapshot = RunStateGraph.create(tenant_id, session_id, run_id, request)
    events = []
    plan = QueryPlan(
        tool_budget=ToolBudget(max_search_queries=1, max_fetches=2),
        searches=(SearchQuery(text="Siemens sustainability report", max_results=2),),
    )
    snapshot, event = RunStateGraph.accept_plan(snapshot, plan)
    events.append(event)
    snapshot, event = RunStateGraph.start_search(snapshot)
    events.append(event)
    hit = SearchHit(
        title="Siemens sustainability report",
        url=checked_url,
        snippet="Official public report",
        rank=1,
    )
    evidence = ExtractedEvidence(
        evidence_id="ev-report",
        source_url=checked_url,
        source_title="Siemens sustainability report",
        summary=evidence_summary,
        quotes=(evidence_summary,),
    )
    snapshot, event = RunStateGraph.record_evidence(
        snapshot, hits=(hit,), evidence=(evidence,)
    )
    events.append(event)
    answer = ScopedAnswer(
        answer_text=CLAIM,
        citations=(
            Citation(
                claim=CLAIM,
                evidence_id="ev-report",
                source_url=checked_url,
            ),
        ),
    )
    snapshot, event = RunStateGraph.draft_answer(snapshot, answer)
    events.append(event)
    snapshot, event = RunStateGraph.complete(snapshot)
    events.append(event)
    return RunResult(
        snapshot=snapshot,
        events=(_created_event(snapshot), *events),
        usage=_usage(failed_pages=failed_pages),
    )


def failed_result(
    *,
    reason: FailureReason = FailureReason.NO_EVIDENCE,
    tenant_id: str = "tenant-one",
    session_id: str = "session-one",
    run_id: str = "run-000002",
    request: str = "Find unsupported Siemens information.",
    partial_evidence: bool = False,
) -> RunResult:
    snapshot = RunStateGraph.create(tenant_id, session_id, run_id, request)
    created = _created_event(snapshot)
    events = []
    if partial_evidence:
        plan = QueryPlan(
            tool_budget=ToolBudget(max_search_queries=1, max_fetches=2),
            searches=(SearchQuery(text="Siemens public report", max_results=2),),
        )
        snapshot, event = RunStateGraph.accept_plan(snapshot, plan)
        events.append(event)
        snapshot, event = RunStateGraph.start_search(snapshot)
        events.append(event)
        hit = SearchHit(
            title="Siemens report",
            url=AnyHttpUrl(SOURCE_URL),
            snippet="Official report",
            rank=1,
        )
        evidence = ExtractedEvidence(
            evidence_id="ev-partial",
            source_url=AnyHttpUrl(SOURCE_URL),
            source_title="Siemens report",
            summary=CLAIM,
        )
        snapshot, event = RunStateGraph.record_evidence(
            snapshot, hits=(hit,), evidence=(evidence,)
        )
        events.append(event)
    snapshot, event = RunStateGraph.fail(snapshot, reason)
    events.append(event)
    return RunResult(
        snapshot=snapshot,
        events=(created, *events),
        usage=_usage(failed_pages=1 if partial_evidence else 0),
    )


def cancelled_result() -> RunResult:
    snapshot = RunStateGraph.create(
        "tenant-one", "session-one", "run-000003", "Find Siemens information."
    )
    created = _created_event(snapshot)
    snapshot, terminal = RunStateGraph.cancel(snapshot)
    return RunResult(
        snapshot=snapshot,
        events=(created, terminal),
        usage=_usage(pages=0),
    )


def reflection(
    *,
    tenant_id: str = "tenant-one",
    session_id: str = "session-one",
    run_id: str = "run-000001",
    request: str = "Find the Siemens 2025 sustainability report.",
) -> RunReflection:
    return reflect_run(
        completed_result(
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
            request=request,
        )
    )


def _created_event(snapshot: RunSnapshot) -> PublicEvent:
    return PublicEvent(
        tenant_id=snapshot.tenant_id,
        session_id=snapshot.session_id,
        run_id=snapshot.run_id,
        event_type=EventType.RUN_CREATED,
        message="Created bounded research run",
    )


def _usage(*, failed_pages: int = 0, pages: int = 2) -> RunUsage:
    return RunUsage(
        elapsed_seconds=0.5,
        iterations=6,
        search_queries=1,
        pages=max(pages, failed_pages),
        failed_pages=failed_pages,
        raw_bytes_reserved=128,
        decoded_bytes=64,
        model_calls=2,
        model_attempts=2,
        tokens=512,
    )
