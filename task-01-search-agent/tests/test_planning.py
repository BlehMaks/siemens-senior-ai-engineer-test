from __future__ import annotations

from typing import TypeVar

import pytest
from pydantic import BaseModel, ValidationError

from search_agent import (
    AssistancePolicy,
    FakeStructuredChatProvider,
    OptionalAssistance,
    PlanningDecision,
    PlanningPolicyError,
    ProviderMessage,
    ProviderMetadata,
    ProviderResponseError,
    ProviderResult,
    QueryPlanner,
    SearchQuery,
    TaskCategory,
    ToolBudget,
)
from search_agent.contracts import QueryPlan

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


@pytest.mark.asyncio
async def test_query_planner_accepts_scoped_search_plan() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Find the current Siemens sustainability report.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 2},
                    "searches": [
                        {
                            "text": "siemens sustainability report 2026",
                            "max_results": 2,
                        },
                    ],
                },
            }
        ]
    )

    decision = await QueryPlanner(provider).plan(
        "Find the current Siemens sustainability report for 2026."
    )

    assert decision.task_category is TaskCategory.COMPANY_RESEARCH
    assert decision.query_plan == QueryPlan(
        tool_budget=ToolBudget(max_search_queries=1, max_fetches=2),
        searches=(
            SearchQuery(text="siemens sustainability report 2026", max_results=2),
        ),
    )


@pytest.mark.asyncio
async def test_query_planner_rejects_prohibited_tool_requests_before_provider_call() -> (
    None
):
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Should not be used.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [{"text": "siemens report", "max_results": 1}],
                },
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="prohibited capability: browser"):
        await QueryPlanner(provider).plan(
            "Use browser automation to find the latest Siemens sustainability report."
        )

    assert provider.calls == []


@pytest.mark.asyncio
async def test_query_planner_rejects_scope_creep_from_model_output() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Find the Siemens report.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 2},
                    "searches": [{"text": "weather in berlin today", "max_results": 2}],
                },
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="stay scoped"):
        await QueryPlanner(provider).plan(
            "Find the latest Siemens sustainability report."
        )


@pytest.mark.asyncio
async def test_query_planner_accepts_ambiguous_requests_without_search() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "clarification",
                "requires_search": False,
                "answer_focus": "Ask which report or year the user wants.",
            }
        ]
    )

    decision = await QueryPlanner(provider).plan("Can you compare the report?")

    assert decision == PlanningDecision(
        task_category=TaskCategory.CLARIFICATION,
        requires_search=False,
        answer_focus="Ask the user to clarify the original request.",
    )


def test_assistance_policy_requires_answer_before_follow_ups() -> None:
    assistance = OptionalAssistance(
        offer="I can compare this report with the previous year next.",
        follow_up_queries=("compare siemens sustainability report 2026",),
    )

    with pytest.raises(PlanningPolicyError, match="requested answer first"):
        AssistancePolicy.validate(
            answer_completed=False,
            request="Find the latest Siemens sustainability report.",
            assistance=assistance,
        )


def test_assistance_policy_rejects_unrelated_follow_ups() -> None:
    assistance = OptionalAssistance(
        offer="I can look at a related topic next.",
        follow_up_queries=("weather in berlin tomorrow",),
    )

    with pytest.raises(PlanningPolicyError, match="tied to the answered request"):
        AssistancePolicy.validate(
            answer_completed=True,
            request="Find the latest Siemens sustainability report.",
            assistance=assistance,
        )


@pytest.mark.asyncio
async def test_planner_rejects_prohibited_capability_added_by_model() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Find the report.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [{"text": "siemens secret api key", "max_results": 1}],
                },
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="prohibited capability"):
        await QueryPlanner(provider).plan(
            "Find the current Siemens sustainability report."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prohibited_query",
    [
        "sustainability report api     key",
        "sustainability report api-key",
        "sustainability report api\u200bkey",
        "sustainability report \uff41\uff50\uff49\uff3f\uff4b\uff45\uff59",
        "sustainability report browsers",
        "sustainability report shells",
        "sustainability report terminals",
    ],
)
async def test_planner_normalizes_obfuscated_prohibited_capabilities(
    prohibited_query: str,
) -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Find the sustainability report.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [
                        {
                            "text": prohibited_query,
                            "max_results": 1,
                        }
                    ],
                },
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="prohibited capability"):
        await QueryPlanner(provider).plan(
            "Find the current Siemens sustainability report."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_text", "query"),
    [
        (
            "Find the current Siemens sustainability report.",
            "siemens stock price forecast",
        ),
        (
            "Review https://www.siemens.com/sustainability/report",
            "https://attacker.example/unrelated",
        ),
        (
            "Find the current Siemens sustainability report.",
            "sustainability weather forecast",
        ),
    ],
)
async def test_planner_rejects_generic_token_scope_bypass(
    request_text: str, query: str
) -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Research the requested subject.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [{"text": query, "max_results": 1}],
                },
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="stay scoped"):
        await QueryPlanner(provider).plan(request_text)


@pytest.mark.asyncio
async def test_planner_accepts_exact_unicode_scope() -> None:
    request = "сравнить отчёт устойчивого развития"
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": request,
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [{"text": request, "max_results": 1}],
                },
            }
        ]
    )

    decision = await QueryPlanner(provider).plan(request)

    assert decision.query_plan is not None
    assert decision.query_plan.searches[0].text == request


@pytest.mark.asyncio
async def test_single_subject_token_cannot_pad_an_unrelated_search() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Research Siemens jobs.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [{"text": "jobs weather", "max_results": 1}],
                },
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="stay scoped"):
        await QueryPlanner(provider).plan("Siemens jobs")


@pytest.mark.asyncio
async def test_action_scope_allows_year_but_rejects_another_topic() -> None:
    valid_provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Research Siemens jobs.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [{"text": "jobs 2026", "max_results": 1}],
                },
            }
        ]
    )

    decision = await QueryPlanner(valid_provider).plan("Siemens jobs")

    assert decision.query_plan is not None
    assert decision.query_plan.searches[0].text == "jobs 2026"

    padded_provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Find the sustainability report.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [
                        {
                            "text": "sustainability report celebrity",
                            "max_results": 1,
                        }
                    ],
                },
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="stay scoped"):
        await QueryPlanner(padded_provider).plan(
            "Find the Siemens sustainability report."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("padding", ["666", "spy", "war"])
async def test_action_scope_rejects_short_added_tokens(padding: str) -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Find the sustainability report.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [
                        {
                            "text": f"sustainability report {padding}",
                            "max_results": 1,
                        }
                    ],
                },
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="stay scoped"):
        await QueryPlanner(provider).plan("Find the Siemens sustainability report.")


def test_assistance_offer_cannot_escalate_capabilities() -> None:
    assistance = OptionalAssistance(
        offer="I can open a shell and retrieve API secrets next.",
    )

    with pytest.raises(PlanningPolicyError, match="prohibited capability"):
        AssistancePolicy.validate(
            answer_completed=True,
            request="Find the latest Siemens sustainability report.",
            assistance=assistance,
        )


def test_single_subject_token_cannot_pad_unrelated_assistance() -> None:
    assistance = OptionalAssistance(
        offer="I can use jobs to research Berlin weather forecasts.",
    )

    with pytest.raises(PlanningPolicyError, match="stay tied"):
        AssistancePolicy.validate(
            answer_completed=True,
            request="Siemens jobs",
            assistance=assistance,
        )

    allowed = OptionalAssistance(
        offer="jobs",
        follow_up_queries=("jobs 2026",),
    )
    assert (
        AssistancePolicy.validate(
            answer_completed=True,
            request="Siemens jobs",
            assistance=allowed,
        )
        == allowed
    )


def test_search_required_decision_needs_nonempty_plan() -> None:
    with pytest.raises(ValidationError, match="at least one search"):
        PlanningDecision(
            task_category=TaskCategory.COMPANY_RESEARCH,
            requires_search=True,
            answer_focus="Find the report.",
            query_plan=QueryPlan(
                tool_budget=ToolBudget(max_search_queries=0, max_fetches=0),
                searches=(),
            ),
        )


def test_direct_reply_cannot_require_search() -> None:
    with pytest.raises(ValidationError, match="task category must agree"):
        PlanningDecision(
            task_category=TaskCategory.DIRECT_REPLY,
            requires_search=True,
            answer_focus="Search for the report.",
            query_plan=QueryPlan(
                tool_budget=ToolBudget(max_search_queries=1, max_fetches=1),
                searches=(SearchQuery(text="sustainability report", max_results=1),),
            ),
        )


@pytest.mark.asyncio
async def test_planner_rejects_coerced_model_types() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": 1,
                "answer_focus": "Find the report.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": "1", "max_fetches": "1"},
                    "searches": [{"text": "sustainability report", "max_results": "1"}],
                },
            }
        ]
    )

    with pytest.raises(ProviderResponseError, match="did not match schema"):
        await QueryPlanner(provider).plan("Find the Siemens sustainability report.")


@pytest.mark.asyncio
async def test_planner_maps_public_contract_failure_to_provider_error() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Find the Siemens sustainability report.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [{"text": "x", "max_results": 1}],
                },
            }
        ]
    )

    with pytest.raises(ProviderResponseError, match="public planning contract"):
        await QueryPlanner(provider).plan("Find the Siemens sustainability report.")


@pytest.mark.asyncio
async def test_planner_rejects_unrelated_generated_answer_focus() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Summarize the weather forecast.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [
                        {"text": "siemens sustainability report", "max_results": 1}
                    ],
                },
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="answer focus"):
        await QueryPlanner(provider).plan("Find the Siemens sustainability report.")


@pytest.mark.asyncio
async def test_direct_reply_allows_semantic_answer_focus() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "direct_reply",
                "requires_search": False,
                "answer_focus": "Explain artificial intelligence.",
            }
        ]
    )

    decision = await QueryPlanner(provider).plan("What is AI?")

    assert decision.task_category is TaskCategory.DIRECT_REPLY


@pytest.mark.asyncio
async def test_direct_reply_rejects_unrelated_answer_focus() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "direct_reply",
                "requires_search": False,
                "answer_focus": "Explain the Berlin weather forecast.",
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="answer focus"):
        await QueryPlanner(provider).plan("What is AI?")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_text", "answer_focus"),
    [
        ("What is AI?", "Explain apple inventory."),
        ("What is ML?", "Explain manage logistics."),
    ],
)
async def test_direct_reply_rejects_false_acronym_expansion(
    request_text: str, answer_focus: str
) -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "direct_reply",
                "requires_search": False,
                "answer_focus": answer_focus,
            }
        ]
    )

    with pytest.raises(PlanningPolicyError, match="answer focus"):
        await QueryPlanner(provider).plan(request_text)


@pytest.mark.asyncio
async def test_clarification_uses_a_fixed_safe_focus() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "clarification",
                "requires_search": False,
                "answer_focus": "Ask which reports should be compared.",
            }
        ]
    )

    decision = await QueryPlanner(provider).plan("Compare them")

    assert decision.answer_focus == "Ask the user to clarify the original request."


@pytest.mark.asyncio
async def test_clarification_discards_safe_generated_assistance() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "clarification",
                "requires_search": False,
                "answer_focus": "Ask which reports should be compared.",
                "assistance": {
                    "offer": "Discuss unrelated Berlin weather.",
                    "follow_up_queries": ["Berlin weather 2026"],
                },
            }
        ]
    )

    decision = await QueryPlanner(provider).plan("Compare them")

    assert decision.answer_focus == "Ask the user to clarify the original request."
    assert decision.assistance is None


class MalformedEnvelope(BaseModel):
    unrelated: str


class MalformedResultProvider:
    async def generate_structured(
        self,
        *,
        messages: tuple[ProviderMessage, ...],
        response_model: type[ResponseModelT],
        temperature: float = 0.0,
    ) -> ProviderResult:
        del messages, response_model, temperature
        return ProviderResult(
            response=MalformedEnvelope(unrelated="not a planning draft"),
            metadata=ProviderMetadata(
                provider_name="malformed",
                model_name="malformed",
                attempt_count=1,
            ),
        )


@pytest.mark.asyncio
async def test_malformed_provider_result_maps_to_typed_error() -> None:
    with pytest.raises(ProviderResponseError, match="planner output"):
        await QueryPlanner(MalformedResultProvider()).plan(
            "Find the Siemens sustainability report."
        )
