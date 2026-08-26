from __future__ import annotations

import pytest

from search_agent import (
    AssistancePolicy,
    FakeStructuredChatProvider,
    OptionalAssistance,
    PlanningDecision,
    PlanningPolicyError,
    QueryPlanner,
    SearchQuery,
    TaskCategory,
    ToolBudget,
)
from search_agent.contracts import QueryPlan


@pytest.mark.asyncio
async def test_query_planner_accepts_scoped_search_plan() -> None:
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": "Find the current Siemens sustainability report.",
                "query_plan": {
                    "tool_budget": {"max_search_queries": 2, "max_fetches": 4},
                    "searches": [
                        {
                            "text": "siemens sustainability report 2026",
                            "max_results": 2,
                        },
                        {
                            "text": "siemens sustainability targets 2026",
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
        tool_budget=ToolBudget(max_search_queries=2, max_fetches=4),
        searches=(
            SearchQuery(text="siemens sustainability report 2026", max_results=2),
            SearchQuery(text="siemens sustainability targets 2026", max_results=2),
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
        answer_focus="Ask which report or year the user wants.",
    )


def test_assistance_policy_requires_answer_before_follow_ups() -> None:
    assistance = OptionalAssistance(
        offer="I can compare this report with the previous year next.",
        follow_up_queries=("compare siemens sustainability reports 2025 2026",),
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
