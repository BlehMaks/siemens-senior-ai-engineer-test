"""Deterministic checks for typed SSE frames and resume cursors."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_api.ports import RunState
from agent_api.schemas import (
    SSE_HEARTBEAT,
    RunEvent,
    RunEventType,
    RunFailure,
    RunFailureCode,
    encode_sse,
    parse_last_event_id,
)
from search_agent.contracts import Citation, ScopedAnswer
from search_agent.memory.contracts import contains_sensitive_memory_text

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
MAX_SEQUENCE = 9_223_372_036_854_775_807


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


def event_values(event_type: RunEventType, state: RunState) -> dict[str, object]:
    return {
        "sequence": 7,
        "run_id": "run-one",
        "event_type": event_type,
        "state": state,
        "occurred_at": NOW,
        "message": "Run state changed.",
    }


@pytest.mark.parametrize(
    ("event_type", "state", "extra"),
    [
        (RunEventType.COMPLETED, RunState.COMPLETED, {"answer": answer()}),
        (
            RunEventType.FAILED,
            RunState.FAILED,
            {
                "failure": RunFailure(
                    code=RunFailureCode.EXECUTION_FAILED,
                    message="Run failed within the configured boundary.",
                    retryable=False,
                )
            },
        ),
        (RunEventType.CANCELLED, RunState.CANCELLED, {}),
        (
            RunEventType.EXPIRED,
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
def test_every_terminal_event_has_a_valid_typed_frame(
    event_type: RunEventType,
    state: RunState,
    extra: dict[str, object],
) -> None:
    event = RunEvent.model_validate({**event_values(event_type, state), **extra})

    frame = encode_sse(event).decode()
    lines = frame[:-2].split("\n")
    payload = json.loads(lines[2].removeprefix("data: "))

    assert lines[:2] == ["id: 7", f"event: {event_type.value}"]
    assert payload["event_type"] == event_type.value
    assert payload["state"] == state.value
    assert "tenant_id" not in payload


@pytest.mark.parametrize(
    ("event_type", "state"),
    [
        (RunEventType.STATUS, RunState.COMPLETED),
        (RunEventType.COMPLETED, RunState.FAILED),
        (RunEventType.FAILED, RunState.FAILED),
        (RunEventType.EXPIRED, RunState.EXPIRED),
    ],
)
def test_events_reject_impossible_lifecycle_shapes(
    event_type: RunEventType, state: RunState
) -> None:
    with pytest.raises(ValidationError):
        RunEvent.model_validate(event_values(event_type, state))


def test_sse_json_escaping_prevents_crlf_or_data_line_injection() -> None:
    message = "first line\r\ndata: forged\n\nevent: forged"
    event = RunEvent.model_validate(
        {**event_values(RunEventType.STATUS, RunState.RUNNING), "message": message}
    )

    frame = encode_sse(event).decode()
    lines = frame[:-2].split("\n")
    payload = json.loads(lines[2].removeprefix("data: "))

    assert len(lines) == 3
    assert "\r" not in frame
    assert payload["message"] == message


def test_encoder_revalidates_constructed_models_before_emitting() -> None:
    unchecked = RunEvent.model_construct(
        sequence=0,
        run_id="run-one",
        event_type=RunEventType.STATUS,
        state=RunState.RUNNING,
        occurred_at=NOW,
        message="Run state changed.",
    )

    with pytest.raises(ValidationError):
        encode_sse(unchecked)


def test_heartbeat_is_an_sse_comment_without_an_event_id() -> None:
    assert SSE_HEARTBEAT == b": heartbeat\n\n"


def test_last_event_id_accepts_only_positive_bounded_decimal_sequences() -> None:
    assert parse_last_event_id(None) is None
    assert parse_last_event_id("1") == 1
    assert parse_last_event_id(str(MAX_SEQUENCE)) == MAX_SEQUENCE

    for invalid in (
        "",
        "0",
        "01",
        "+1",
        "-1",
        " 1",
        "1 ",
        "1\r\nLast-Event-ID: 2",
        str(MAX_SEQUENCE + 1),
        "9" * 20,
    ):
        with pytest.raises(ValueError, match="invalid Last-Event-ID"):
            parse_last_event_id(invalid)


@settings(max_examples=80, derandomize=True, deadline=None)
@given(st.text(min_size=1, max_size=240))
def test_arbitrary_public_messages_cannot_create_extra_sse_fields(
    message: str,
) -> None:
    assume(bool(message.strip()))
    assume(not contains_sensitive_memory_text(message))
    try:
        event = RunEvent.model_validate(
            {
                **event_values(RunEventType.STATUS, RunState.RUNNING),
                "message": message,
            }
        )
    except ValidationError:
        return

    frame = encode_sse(event).decode()
    lines = frame[:-2].split("\n")
    payload = json.loads(lines[2].removeprefix("data: "))

    assert len(lines) == 3
    assert payload["message"] == event.message
