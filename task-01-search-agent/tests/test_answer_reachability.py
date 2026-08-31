"""Regressions for defects that made ordinary requests unanswerable."""

from __future__ import annotations

import pytest

from search_agent.planning import (
    _FIXED_CONVERSATIONAL_FOCUS,
    AnswerScopePolicy,
    PlanningPolicyError,
    QueryPlanner,
    TaskCategory,
    is_conversational_request,
)
from search_agent.providers import FakeStructuredChatProvider
from search_agent.runner import _citable_spans
from search_agent.tools.search import search_text_for


@pytest.mark.parametrize(
    "request_text",
    ["how are you today?", "hi there", "thanks!", "who are you?"],
)
def test_small_talk_is_recognised(request_text: str) -> None:
    assert is_conversational_request(request_text)


@pytest.mark.parametrize(
    "request_text",
    [
        "how is the weather today?",
        "what wikipedia says about germany?",
        "how are Siemens turbines built?",
    ],
)
def test_real_questions_are_not_small_talk(request_text: str) -> None:
    assert not is_conversational_request(request_text)


@pytest.mark.asyncio
async def test_small_talk_answers_directly_instead_of_searching() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "direct_reply",
                "requires_search": False,
                "answer_focus": "I am well, thank you for asking",
            }
        ]
    )
    planner = QueryPlanner(provider, repair_invalid_company_plans=True)

    decision = await planner.plan("how are you today?")

    assert decision.task_category is TaskCategory.DIRECT_REPLY
    assert decision.requires_search is False
    assert decision.answer_focus == "I am well, thank you for asking"


@pytest.mark.asyncio
async def test_small_talk_echo_is_replaced_by_a_real_reply() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "direct_reply",
                "requires_search": False,
                "answer_focus": "How are you today",
            }
        ]
    )
    planner = QueryPlanner(provider, repair_invalid_company_plans=True)

    decision = await planner.plan("how are you today?")

    assert decision.task_category is TaskCategory.DIRECT_REPLY
    assert decision.answer_focus == _FIXED_CONVERSATIONAL_FOCUS


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("what wikipedia says about germany?", "wikipedia germany"),
        ("where can I find mcdonalds in munich?", "find mcdonalds munich"),
        ("Siemens sustainability report 2024", "Siemens sustainability report 2024"),
    ],
)
def test_search_text_keeps_only_topic_words(request_text: str, expected: str) -> None:
    assert search_text_for(request_text) == expected


def test_search_text_stays_a_subset_of_the_request() -> None:
    request = "where can I find mcdonalds in munich?"
    request_words = set(request.lower().replace("?", "").split())

    assert set(search_text_for(request).lower().split()) <= request_words


def test_search_text_survives_an_all_common_word_request() -> None:
    assert search_text_for("what is it?") == "what is it"


def test_citable_spans_offer_each_sentence_of_a_quote() -> None:
    quote = (
        "Munich currently reports 18 degrees Celsius with light rain. "
        "The forecast for tomorrow is dry and sunny all day long."
    )

    spans = _citable_spans((quote,))

    assert quote in spans
    assert "Munich currently reports 18 degrees Celsius with light rain." in spans
    assert "The forecast for tomorrow is dry and sunny all day long." in spans
    # Every offered span stays a verbatim substring, so the validator is unchanged.
    assert all(span in quote for span in spans)


def test_citable_spans_drop_fragments_too_short_to_carry_a_claim() -> None:
    assert _citable_spans(("Yes. No. Maybe.",)) == []
