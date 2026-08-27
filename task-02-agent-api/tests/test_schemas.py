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
    FieldIssue,
    LastEventId,
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
_PASSWORD_FIELD = "pass" + "word"
_CLIENT_SECRET_FIELD = "client_" + "secret"
_DOUBLE_ENCODED_CLIENT_SECRET_FIELD = _CLIENT_SECRET_FIELD.replace("_", "%255f")
_DISCLOSURE_SENTINEL = "stolen-production-credential"
_DOCUMENTED_CREDENTIAL_TOKENS = (
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
)
DEFAULT_IGNORABLE_BOUNDARIES = (
    "\u00ad",
    "\u034f",
    "\u061c",
    "\u115f",
    "\u1160",
    "\u17b4",
    "\u17b5",
    "\u180b",
    "\u180f",
    "\u200b",
    "\u200f",
    "\u202a",
    "\u202e",
    "\u2060",
    "\u206f",
    "\u3164",
    "\ufe00",
    "\ufe0f",
    "\ufeff",
    "\uffa0",
    "\ufff0",
    "\ufff8",
    "\U0001bca0",
    "\U0001bca3",
    "\U0001d173",
    "\U0001d17a",
    "\U000e0000",
    "\U000e0fff",
)


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


@pytest.mark.parametrize(
    "source_url",
    [
        "https://alice:private-pass@example.com/report",
        "http://127.0.0.1:8080/private",
        "https://example.com/report#access-token",
        "https://example.com/report?api_key=private",
        f"https://example.com/report/{_PASSWORD_FIELD}={_DISCLOSURE_SENTINEL}",
        f"https://example.com/report/{_PASSWORD_FIELD}%253D{_DISCLOSURE_SENTINEL}",
        "https://example.com/report/pass%EF%B8%8Fword=stolen-production-credential",
        f"https://example.com/report?q={_PASSWORD_FIELD}%253D{_DISCLOSURE_SENTINEL}",
        f"https://example.com/report?{_CLIENT_SECRET_FIELD}={_DISCLOSURE_SENTINEL}",
        f"https://example.com/report?{_DOUBLE_ENCODED_CLIENT_SECRET_FIELD}={_DISCLOSURE_SENTINEL}",
        "https://example.com/report?q=sk-testcredential12345678",
        f"https://example.com/report/{_CLIENT_SECRET_FIELD}={_DISCLOSURE_SENTINEL}",
        f"https://example.com/report/{_DOUBLE_ENCODED_CLIENT_SECRET_FIELD}%253d{_DISCLOSURE_SENTINEL}",
        f"https://example.com/report?next={_CLIENT_SECRET_FIELD}={_DISCLOSURE_SENTINEL}",
        f"https://example.com/report?next={_DOUBLE_ENCODED_CLIENT_SECRET_FIELD}%253d{_DISCLOSURE_SENTINEL}",
        f"https://example.com/report?scope=public%26{_CLIENT_SECRET_FIELD}%3d{_DISCLOSURE_SENTINEL}",
        f"https://example.com/report?scope=public%2526{_CLIENT_SECRET_FIELD}%253d{_DISCLOSURE_SENTINEL}",
        f"https://example.com/report?scope=public;{_CLIENT_SECRET_FIELD}={_DISCLOSURE_SENTINEL}",
        "https://example.com/report?download=gho_1234567890abcdefghijklmnopqrstuvwxyz",
        "https://example.com/report?download=github_pat_11AA000000000000000000_0123456789abcdefghijklmnopqrstuvwxyz",
        "https://example.com/report?download=xoxp-1234567890-1234567890-1234567890-abcdef",
        "https://example.com/report?download=ya29.a0AfH6SMB1234567890abcdefghijklmnopqrstuv",
        "https://sk-admin-1234abcd.example.com/report",
        *(
            f"https://example.com/report?download={token}"
            for token in _DOCUMENTED_CREDENTIAL_TOKENS
        ),
    ],
)
def test_public_answer_rejects_non_public_or_sensitive_source_urls(
    source_url: str,
) -> None:
    unsafe_answer = ScopedAnswer(
        answer_text="The response must not publish a sensitive source URL.",
        citations=(
            Citation.model_validate(
                {
                    "claim": "The source supports the answer.",
                    "evidence_id": "ev-source-one",
                    "source_url": source_url,
                }
            ),
        ),
    )

    with pytest.raises(ValidationError, match="citation URL"):
        RunStatusResponse.model_validate(
            {**status_values(RunState.COMPLETED), "answer": unsafe_answer}
        )


@pytest.mark.parametrize(
    "source_url",
    [
        "https://example.com/oauth/client-secret-management",
        "https://example.com/report?topic=client_secret_management",
        "https://example.com/report?q=credential+rotation+guide",
        "https://example.com/companies/sk-telecom-sustainability-report",
        "https://example.com/reports/sk-admin-dashboard",
        "https://example.com/r%C3%A9sum%C3%A9?q=cafe%CC%81",
        "https://example.com/report?completion=100%25",
    ],
)
def test_public_answer_allows_safe_url_topics(source_url: str) -> None:
    safe_answer = ScopedAnswer(
        answer_text="The documented answer is supported by the cited source.",
        citations=(
            Citation.model_validate(
                {
                    "claim": "The source supports the answer.",
                    "evidence_id": "ev-source-one",
                    "source_url": source_url,
                }
            ),
        ),
    )

    response = RunStatusResponse.model_validate(
        {**status_values(RunState.COMPLETED), "answer": safe_answer}
    )

    assert str(response.answer.citations[0].source_url) == source_url


@pytest.mark.parametrize(
    "unsafe_answer",
    [
        answer().model_copy(
            update={"answer_text": "Authoriza" + "tion: Bearer stolen-credential"}
        ),
        answer().model_copy(
            update={"answer_text": "Pass\u200bword = stolen-production-credential"}
        ),
        answer().model_copy(
            update={"answer_text": "Pass\u034fword = stolen-production-credential"}
        ),
        answer().model_copy(
            update={"answer_text": "Pass\ufe0fword = stolen-production-credential"}
        ),
        answer().model_copy(
            update={
                "citations": (
                    answer()
                    .citations[0]
                    .model_copy(update={"claim": "Pass" + "word = stolen-credential"}),
                )
            }
        ),
        answer().model_copy(
            update={
                "assistance": OptionalAssistance(
                    offer="System prompt is hidden production policy.",
                    follow_up_queries=("Find more public evidence.",),
                )
            }
        ),
        answer().model_copy(
            update={
                "assistance": OptionalAssistance(
                    offer="I can help with public evidence.",
                    follow_up_queries=("Access to" + "ken: stolen-credential",),
                )
            }
        ),
    ],
    ids=(
        "answer",
        "zero-width-answer",
        "grapheme-joiner-answer",
        "variation-selector-answer",
        "citation-claim",
        "assistance-offer",
        "follow-up-query",
    ),
)
def test_public_answer_rejects_sensitive_text_in_every_channel(
    unsafe_answer: ScopedAnswer,
) -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        RunStatusResponse.model_validate(
            {**status_values(RunState.COMPLETED), "answer": unsafe_answer}
        )


@pytest.mark.parametrize(
    "invisible_mark",
    DEFAULT_IGNORABLE_BOUNDARIES,
    ids=[f"U+{ord(mark):04X}" for mark in DEFAULT_IGNORABLE_BOUNDARIES],
)
def test_default_ignorable_boundaries_cannot_split_sensitive_markers(
    invisible_mark: str,
) -> None:
    unsafe_answer = answer().model_copy(
        update={
            "answer_text": f"Pass{invisible_mark}word = stolen-production-credential"
        }
    )

    with pytest.raises(ValidationError, match="sensitive"):
        RunStatusResponse.model_validate(
            {**status_values(RunState.COMPLETED), "answer": unsafe_answer}
        )


@pytest.mark.parametrize(
    "public_text",
    [
        "Cafe\u0301 and re\u0301sume\u0301 evidence are public.",
        "Résumé naïve façade: documented public evidence.",
        "Исследование подтверждено открытыми источниками.",
        "अनुसंधान सार्वजनिक स्रोतों से समर्थित है।",
        "إجابةٌ عامةٌ مدعومةٌ بمصادرَ منشورةٍ.",
        "公開情報に基づく回答です。",
    ],
)
def test_ordinary_combining_marks_and_international_text_remain_public(
    public_text: str,
) -> None:
    public_answer = answer().model_copy(update={"answer_text": public_text})

    response = RunStatusResponse.model_validate(
        {**status_values(RunState.COMPLETED), "answer": public_answer}
    )

    assert response.answer is not None
    assert response.answer.answer_text == public_text


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
    for unsafe_message in (
        'Traceback (most recent call last): File "/srv/app.py", line 42',
        "postgresql://admin:p4ssw0rd@db.internal/agent",
    ):
        with pytest.raises(ValidationError, match="sensitive material"):
            RunFailure(
                code=RunFailureCode.EXECUTION_FAILED,
                message=unsafe_message,
                retryable=False,
            )
    with pytest.raises(ValidationError, match="private diagnostic"):
        FieldIssue(field="internal_detail", message="The value is invalid.")


def test_nested_public_error_models_are_revalidated() -> None:
    unsafe_failure = RunFailure.model_construct(
        code="expired",
        message="password=public-leak",
        retryable=False,
    )
    with pytest.raises(ValidationError):
        RunStatusResponse.model_validate(
            {**status_values(RunState.FAILED), "failure": unsafe_failure}
        )

    unsafe_detail = ErrorDetail.model_construct(
        code=ErrorCode.INTERNAL,
        message="password=public-leak",
        correlation_id="correlation-one",
        retryable=False,
        field_issues=(),
    )
    with pytest.raises(ValidationError, match="sensitive material"):
        ErrorEnvelope(error=unsafe_detail)


def test_last_event_header_type_enforces_the_signed_sequence_bound() -> None:
    adapter = TypeAdapter(LastEventId)

    assert adapter.validate_python("9223372036854775807", strict=True) == (
        "9223372036854775807"
    )
    with pytest.raises(ValidationError, match="public bound"):
        adapter.validate_python("9223372036854775808", strict=True)


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
