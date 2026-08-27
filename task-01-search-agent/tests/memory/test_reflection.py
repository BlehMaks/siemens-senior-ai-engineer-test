from __future__ import annotations

import json

import pytest
from pydantic import AnyHttpUrl

from search_agent import (
    Citation,
    EventType,
    FailureReason,
    PublicEvent,
    RunResult,
    RunUsage,
    ScopedAnswer,
)
from search_agent.memory import (
    FailureCode,
    RecoveryStep,
    ReflectionInputError,
    UnresolvedItem,
    reflect_run,
)
from search_agent.memory.contracts import contains_sensitive_memory_text

from .helpers import CLAIM, cancelled_result, completed_result, failed_result


def test_completed_reflection_has_a_stable_bounded_snapshot() -> None:
    reflected = reflect_run(completed_result())

    assert reflected.model_dump(mode="json") == {
        "schema_version": 1,
        "tenant_id": "tenant-one",
        "session_id": "session-one",
        "run_id": "run-000001",
        "requested_outcome": "Find the Siemens 2025 sustainability report.",
        "actions": [
            "run_created",
            "plan_accepted",
            "search_started",
            "evidence_ready",
            "answer_drafted",
            "run_completed",
        ],
        "failures": [],
        "recovery_steps": [],
        "completion_evidence": [
            {
                "evidence_id": "ev-report",
                "source_url": "https://www.siemens.com/reports/sustainability-2025",
            }
        ],
        "unresolved_items": [],
        "outcome": "completed",
        "usage": {
            "elapsed_seconds": 0.5,
            "iterations": 6,
            "search_queries": 1,
            "pages": 2,
            "failed_pages": 0,
            "raw_bytes_reserved": 128,
            "decoded_bytes": 64,
            "model_calls": 2,
            "model_attempts": 2,
            "tokens": 512,
        },
    }


def test_failed_cancelled_and_partial_runs_still_reflect() -> None:
    failed = reflect_run(failed_result())
    cancelled = reflect_run(cancelled_result())
    partial = reflect_run(
        failed_result(
            reason=FailureReason.VALIDATION_FAILED,
            partial_evidence=True,
            run_id="run-000004",
        )
    )

    assert failed.outcome.value == "failed"
    assert failed.unresolved_items == (UnresolvedItem.NO_EVIDENCE,)
    assert failed.failures[0].code is FailureCode.NO_EVIDENCE
    assert failed.completion_evidence == ()
    assert cancelled.outcome.value == "cancelled"
    assert cancelled.unresolved_items == (UnresolvedItem.CANCELLED,)
    assert partial.completion_evidence == ()
    assert partial.recovery_steps == (RecoveryStep.CONTINUED_WITH_REMAINING_EVIDENCE,)
    assert {failure.code for failure in partial.failures} == {
        FailureCode.PAGE_PROCESSING_FAILED,
        FailureCode.VALIDATION_FAILED,
    }


def test_request_is_redacted_and_arbitrary_public_text_is_not_retained() -> None:
    request = (
        "Find Siemens. Bearer bearer-value; api_key=key-value; "
        "system prompt: hidden instructions. raw page: private body. "
        "MODEL-PRIVATE-TAIL ValueError: adapter detail."
    )
    result = completed_result(
        request=request,
        evidence_summary="Raw page: credential-private-sentinel. " + CLAIM,
    )
    first = result.events[0]
    event = PublicEvent(
        tenant_id=first.tenant_id,
        session_id=first.session_id,
        run_id=first.run_id,
        event_type=EventType.RUN_CREATED,
        message="Bearer event-private-token and RuntimeError: private detail",
    )
    result = RunResult(
        snapshot=result.snapshot,
        events=(event, *result.events[1:]),
        usage=result.usage,
    )

    serialized = reflect_run(result).model_dump_json().casefold()

    for forbidden in (
        "bearer-value",
        "key-value",
        "system prompt",
        "hidden instructions",
        "raw page",
        "private body",
        "model-private-tail",
        "valueerror",
        "adapter detail",
        "event-private-token",
        "runtimeerror",
        "credential-private-sentinel",
    ):
        assert forbidden not in serialized
    assert "[redacted]" in serialized


@pytest.mark.parametrize(
    ("raw_request", "secret"),
    [
        ("Find Siemens using credential=private-credential", "private-credential"),
        ("Find Siemens using access_token=private-access", "private-access"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        (
            "Find https://alice:private-pass@example.com/report",
            "private-pass",
        ),
        (
            "Find gho_1234567890abcdefghijklmnopqrstuvwxyz",
            "gho_1234567890abcdefghijklmnopqrstuvwxyz",
        ),
        (
            "Find github_pat_11AA000000000000000000_0123456789abcdefghijklmnopqrstuvwxyz",
            "github_pat_11AA000000000000000000_0123456789abcdefghijklmnopqrstuvwxyz",
        ),
        (
            "Find xoxp-1234567890-1234567890-1234567890-abcdef",
            "xoxp-1234567890-1234567890-1234567890-abcdef",
        ),
        (
            "Find ya29.a0AfH6SMB1234567890abcdefghijklmnopqrstuv",
            "ya29.a0AfH6SMB1234567890abcdefghijklmnopqrstuv",
        ),
    ],
)
def test_request_redacts_common_credential_forms(raw_request: str, secret: str) -> None:
    serialized = reflect_run(completed_result(request=raw_request)).model_dump_json()

    assert secret.casefold() not in serialized.casefold()


@pytest.mark.parametrize(
    "token",
    [
        "ABIA1234567890ABCDEF",
        "ASIA1234567890ABCDEF",
        "sk-admin-1234abcd",
        *(
            f"{prefix}-{'A' * 24}"
            for prefix in (
                "gloas",
                "gldt",
                "glrt",
                "glrtr",
                "glcbt",
                "glptt",
                "glft",
                "glimt",
                "glagent",
                "glwt",
                "glsoat",
                "glffct",
            )
        ),
        f"_gitlab_session={'A' * 24}",
    ],
)
def test_shared_memory_rejects_documented_credential_prefixes(token: str) -> None:
    assert contains_sensitive_memory_text(token)
    assert (
        token
        not in reflect_run(
            completed_result(request=f"Find the report using {token}")
        ).model_dump_json()
    )


def test_hostile_containers_subclasses_and_oversized_usage_fail_typed() -> None:
    with_list = completed_result(run_id="run-hostile-list")
    object.__setattr__(with_list, "events", list(with_list.events))
    with pytest.raises(ReflectionInputError, match="strict observable"):
        reflect_run(with_list)

    class EventSubclass(PublicEvent):
        pass

    with_subclass = completed_result(run_id="run-hostile-subclass")
    first = EventSubclass.model_validate(
        with_subclass.events[0].model_dump(mode="python"), strict=True
    )
    with_subclass = RunResult(
        snapshot=with_subclass.snapshot,
        events=(first, *with_subclass.events[1:]),
        usage=with_subclass.usage,
    )
    with pytest.raises(ReflectionInputError, match="strict observable"):
        reflect_run(with_subclass)

    oversized = completed_result(run_id="run-hostile-usage")
    usage = RunUsage(
        elapsed_seconds=0.5,
        iterations=10_000,
        search_queries=1,
        pages=1,
        failed_pages=0,
        raw_bytes_reserved=1,
        decoded_bytes=1,
        model_calls=1,
        model_attempts=1,
        tokens=1,
    )
    oversized = RunResult(
        snapshot=oversized.snapshot,
        events=oversized.events,
        usage=usage,
    )
    with pytest.raises(ReflectionInputError, match="strict observable"):
        reflect_run(oversized)

    too_many_hits = completed_result(run_id="run-hostile-hits")
    snapshot_values = too_many_hits.snapshot.model_dump(mode="python")
    snapshot_values["hits"] = too_many_hits.snapshot.hits * 41
    oversized_snapshot = type(too_many_hits.snapshot).model_validate(
        snapshot_values, strict=True
    )
    too_many_hits = RunResult(
        snapshot=oversized_snapshot,
        events=too_many_hits.events,
        usage=too_many_hits.usage,
    )
    with pytest.raises(ReflectionInputError, match="strict observable"):
        reflect_run(too_many_hits)


def test_too_many_events_and_invalid_completion_provenance_fail() -> None:
    too_many = completed_result(run_id="run-many-events")
    extra = PublicEvent(
        tenant_id=too_many.snapshot.tenant_id,
        session_id=too_many.snapshot.session_id,
        run_id=too_many.snapshot.run_id,
        event_type=EventType.SEARCH_STARTED,
        message="Observable bounded action",
    )
    too_many = RunResult(
        snapshot=too_many.snapshot,
        events=(too_many.events[0], *(extra for _ in range(16)), too_many.events[-1]),
        usage=too_many.usage,
    )
    with pytest.raises(ReflectionInputError, match="strict observable"):
        reflect_run(too_many)

    invalid = completed_result(run_id="run-bad-citation")
    assert invalid.snapshot.answer is not None
    bad_answer = ScopedAnswer(
        answer_text=CLAIM,
        citations=(
            Citation(
                claim=CLAIM,
                evidence_id="ev-report",
                source_url=AnyHttpUrl("https://example.com/fabricated"),
            ),
        ),
    )
    values = invalid.snapshot.model_dump(mode="python")
    values["answer"] = bad_answer
    snapshot = type(invalid.snapshot).model_validate(values, strict=True)
    invalid = RunResult(snapshot=snapshot, events=invalid.events, usage=invalid.usage)
    with pytest.raises(ReflectionInputError, match="provenance"):
        reflect_run(invalid)


@pytest.mark.parametrize(
    "source_url",
    [
        "http://127.0.0.1/private",
        "http://metadata.google.internal/latest",
        "https://www.siemens.com/report?api_key=private-value",
        "https://www.siemens.com/report?q=Bearer%20private-value",
        "https://www.siemens.com/report?credential=private-value",
        "https://www.siemens.com/report?auth=private-value",
        "https://www.siemens.com/report?sig=private-value",
        "https://www.siemens.com/reports/ASIA1234567890ABCDEF",
        "https://www.siemens.com/reports/%2541SIA1234567890ABCDEF",
        "https://www.siemens.com/report?download=%252541SIA1234567890ABCDEF",
        "https://www.siemens.com/report#private-fragment",
        "https://user:password@www.siemens.com/report",
    ],
)
def test_private_or_credentialed_completion_urls_are_not_retained(
    source_url: str,
) -> None:
    result = completed_result(run_id="run-unsafe-url", source_url=source_url)

    with pytest.raises(ReflectionInputError, match="safe to retain"):
        reflect_run(result)


def test_public_completion_url_allows_benign_query_name() -> None:
    reflected = reflect_run(
        completed_result(source_url="https://www.siemens.com/report?monkey=business")
    )

    assert str(reflected.completion_evidence[0].source_url).endswith("monkey=business")


def test_reflection_schema_has_no_prompt_reasoning_or_page_fields() -> None:
    schema = json.dumps(reflect_run(completed_result()).model_json_schema()).casefold()

    assert "prompt" not in schema
    assert "reasoning" not in schema
    assert "raw_page" not in schema
    assert "answer_text" not in schema
