"""Boundary and lifecycle checks for the public HTTP models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from agent_api.ports import RunState
from agent_api.schemas import (
    CancellationResponse,
    CreateSessionRequest,
    DeletionResponse,
    ErrorCode,
    ErrorDetail,
    ErrorEnvelope,
    PageCursor,
    RunFailure,
    RunFailureCode,
    RunStatusResponse,
    RunSubmitRequest,
    SessionListResponse,
    SessionResponse,
)
from search_agent.contracts import Citation, OptionalAssistance, ScopedAnswer

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


def answer() -> ScopedAnswer:
    return ScopedAnswer(
        answer_text="The documented answer is supported by the cited source.",
        citations=(
            Citation.model_validate(
                {
                    "claim": "The source supports the answer.",
                    "evidence_id": "ev-source-one",
                    "source_url": "https://example.com/source",
                }
            ),
        ),
    )


def status_values(state: RunState) -> dict[str, object]:
    return {
        "session_id": "session-one",
        "run_id": "run-one",
        "state": state,
        "created_at": NOW,
        "updated_at": NOW + timedelta(seconds=2),
        "terminal_at": (
            NOW + timedelta(seconds=2)
            if state
            in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
                RunState.EXPIRED,
            }
            else None
        ),
    }


@pytest.mark.parametrize(
    ("state", "extra"),
    [
        (RunState.COMPLETED, {"answer": answer()}),
        (
            RunState.FAILED,
            {
                "failure": RunFailure(
                    code=RunFailureCode.EXECUTION_FAILED,
                    message="Run failed within the configured boundary.",
                    retryable=False,
                )
            },
        ),
        (RunState.CANCELLED, {"cancellation_requested": True}),
        (
            RunState.EXPIRED,
            {
                "failure": RunFailure(
                    code=RunFailureCode.EXPIRED,
                    message="Run expired before work could complete.",
                    retryable=True,
                )
            },
        ),
    ],
)
def test_every_terminal_state_has_a_valid_public_shape(
    state: RunState, extra: dict[str, object]
) -> None:
    response = RunStatusResponse.model_validate({**status_values(state), **extra})

    assert response.state is state
    assert response.terminal_at is not None


@pytest.mark.parametrize(
    "values",
    [
        {**status_values(RunState.COMPLETED)},
        {
            **status_values(RunState.FAILED),
            "answer": answer(),
            "failure": RunFailure(
                code=RunFailureCode.EXECUTION_FAILED,
                message="Run failed safely.",
                retryable=False,
            ),
        },
        {
            **status_values(RunState.EXPIRED),
            "failure": RunFailure(
                code=RunFailureCode.EXECUTION_FAILED,
                message="Run expired safely.",
                retryable=True,
            ),
        },
        {
            **status_values(RunState.RUNNING),
            "terminal_at": NOW + timedelta(seconds=2),
        },
    ],
)
def test_run_status_rejects_inconsistent_lifecycle(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RunStatusResponse.model_validate(values)


@pytest.mark.parametrize(
    "values",
    [
        {**status_values(RunState.CANCELLED)},
        {
            **status_values(RunState.COMPLETED),
            "answer": answer(),
            "cancellation_requested": True,
        },
    ],
)
def test_run_status_rejects_inconsistent_cancellation(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="cancellation"):
        RunStatusResponse.model_validate(values)


def test_public_models_forbid_extra_or_tenant_fields() -> None:
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate({"label": "Research", "tenant_id": "x"})
    with pytest.raises(ValidationError):
        RunSubmitRequest.model_validate(
            {"query": "find a documented answer", "system_prompt": "hidden"}
        )


def test_session_pagination_is_bounded_and_opaque() -> None:
    session = SessionResponse(
        session_id="session-one",
        label="Research",
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(ValidationError):
        SessionListResponse(items=(session,) * 101)
    cursor_adapter = TypeAdapter(PageCursor)
    assert (
        cursor_adapter.validate_python("next_page_123", strict=True) == "next_page_123"
    )
    for invalid in ("short", "space cursor", "cursor=padding"):
        with pytest.raises(ValidationError):
            cursor_adapter.validate_python(invalid, strict=True)


def test_nested_public_answer_collections_are_bounded() -> None:
    citation = answer().citations[0]
    too_many_citations = ScopedAnswer.model_construct(
        answer_text="Bounded answer.",
        citations=tuple(
            Citation.model_validate(
                {
                    "claim": "The source supports the answer.",
                    "evidence_id": f"ev-source-{index}",
                    "source_url": f"https://example.com/source/{index}",
                }
            )
            for index in range(17)
        ),
    )
    with pytest.raises(ValidationError, match="citation limit"):
        RunStatusResponse.model_validate(
            {
                **status_values(RunState.COMPLETED),
                "answer": too_many_citations,
            }
        )

    too_many_follow_ups = ScopedAnswer(
        answer_text="Bounded answer.",
        citations=(citation,),
        assistance=OptionalAssistance(
            offer="Further research is available.",
            follow_up_queries=("find more evidence",) * 9,
        ),
    )
    with pytest.raises(ValidationError, match="follow-up query limit"):
        RunStatusResponse.model_validate(
            {
                **status_values(RunState.COMPLETED),
                "answer": too_many_follow_ups,
            }
        )


def test_public_deletion_count_is_bounded() -> None:
    with pytest.raises(ValidationError):
        DeletionResponse(
            deleted_count=9_223_372_036_854_775_808,
            completed_at=NOW,
        )


def test_cancellation_response_is_self_consistent() -> None:
    accepted = CancellationResponse(
        run_id="run-one",
        state=RunState.RUNNING,
        cancellation_requested=True,
        changed=True,
        requested_at=NOW,
    )
    duplicate = CancellationResponse(
        run_id="run-one",
        state=RunState.RUNNING,
        cancellation_requested=True,
        changed=False,
        requested_at=NOW,
    )
    completed = CancellationResponse(
        run_id="run-one",
        state=RunState.COMPLETED,
        cancellation_requested=False,
        changed=False,
    )

    assert accepted.changed is True
    assert duplicate.changed is False
    assert completed.cancellation_requested is False

    with pytest.raises(ValidationError):
        CancellationResponse(
            run_id="run-one",
            state=RunState.RUNNING,
            cancellation_requested=False,
            changed=True,
        )
    with pytest.raises(ValidationError):
        CancellationResponse(
            run_id="run-one",
            state=RunState.RUNNING,
            cancellation_requested=True,
            changed=False,
        )
    with pytest.raises(ValidationError, match="cancelled runs"):
        CancellationResponse(
            run_id="run-one",
            state=RunState.CANCELLED,
            cancellation_requested=False,
            changed=False,
        )
    with pytest.raises(ValidationError, match="does not match"):
        CancellationResponse(
            run_id="run-one",
            state=RunState.COMPLETED,
            cancellation_requested=True,
            changed=False,
            requested_at=NOW,
        )


def test_safe_error_envelope_has_no_internal_detail_channel() -> None:
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=ErrorCode.UNAVAILABLE,
            message="Service is temporarily unavailable.",
            correlation_id="correlation-one",
            retryable=True,
        )
    )
    assert set(envelope.model_dump()["error"]) == {
        "code",
        "message",
        "correlation_id",
        "retryable",
        "field_issues",
    }
    with pytest.raises(ValidationError, match="sensitive material"):
        ErrorDetail(
            code=ErrorCode.INTERNAL,
            message="ValueError: raw internal detail",
            correlation_id="correlation-one",
            retryable=False,
        )


@pytest.mark.parametrize(
    "timestamp",
    [datetime(2026, 8, 27, 10, 0), NOW.astimezone(timezone(timedelta(hours=2)))],
)
def test_public_timestamps_require_exact_utc(timestamp: datetime) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        SessionResponse(
            session_id="session-one",
            created_at=timestamp,
            updated_at=timestamp,
        )


@settings(max_examples=80, derandomize=True, deadline=None)
@given(st.text(max_size=320))
def test_query_boundary_is_deterministic_for_arbitrary_text(value: str) -> None:
    try:
        request = RunSubmitRequest(query=value)
    except ValidationError:
        return
    assert 3 <= len(request.query) <= 240
    assert request.query == request.query.strip()
