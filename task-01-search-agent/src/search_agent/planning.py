from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .contracts import (
    OptionalAssistance,
    QueryPlan,
    SearchQuery,
    StrictModel,
    ToolBudget,
)
from .providers import ProviderMessage, StructuredChatProvider

PlanningText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=400, strip_whitespace=True),
]

_TOKEN_PATTERN = re.compile(r"[a-z0-9]{4,}")
_FORBIDDEN_REQUEST_MARKERS = (
    "system prompt",
    "secret",
    "api key",
    "browser",
    "playwright",
    "shell",
    "terminal",
)

PLANNING_SYSTEM_PROMPT = (
    "You are a bounded research planner. Use only internet search planning, "
    "never request secrets, system prompts, browser automation, or shell access. "
    "Return only the structured response."
)


class TaskCategory(StrEnum):
    DIRECT_REPLY = "direct_reply"
    CLARIFICATION = "clarification"
    COMPANY_RESEARCH = "company_research"


class PlanningPolicyError(ValueError):
    """Raised when a planning request or model output violates policy bounds."""


class PlanningDecision(StrictModel):
    task_category: TaskCategory
    requires_search: bool
    answer_focus: PlanningText
    query_plan: QueryPlan | None = None
    assistance: OptionalAssistance | None = None

    @model_validator(mode="after")
    def validate_search_shape(self) -> PlanningDecision:
        if self.requires_search and self.query_plan is None:
            msg = "search-required plans must include a query_plan"
            raise ValueError(msg)
        if not self.requires_search and self.query_plan is not None:
            msg = "direct or clarification plans cannot include a query_plan"
            raise ValueError(msg)
        return self


class DraftSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=3, max_length=240)
    max_results: int = Field(ge=1, le=5)


class DraftToolBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_search_queries: int = Field(ge=0, le=8)
    max_fetches: int = Field(ge=0, le=24)


class DraftQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_budget: DraftToolBudget
    searches: list[DraftSearchQuery]


class DraftOptionalAssistance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    offer: str = Field(min_length=1, max_length=400)
    follow_up_queries: list[str] = Field(default_factory=list)


class PlanningDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_category: TaskCategory
    requires_search: bool
    answer_focus: str = Field(min_length=1, max_length=400)
    query_plan: DraftQueryPlan | None = None
    assistance: DraftOptionalAssistance | None = None

    def to_decision(self) -> PlanningDecision:
        return PlanningDecision(
            task_category=self.task_category,
            requires_search=self.requires_search,
            answer_focus=self.answer_focus,
            query_plan=(
                None
                if self.query_plan is None
                else QueryPlan(
                    tool_budget=ToolBudget(
                        max_search_queries=self.query_plan.tool_budget.max_search_queries,
                        max_fetches=self.query_plan.tool_budget.max_fetches,
                    ),
                    searches=tuple(
                        SearchQuery(text=search.text, max_results=search.max_results)
                        for search in self.query_plan.searches
                    ),
                )
            ),
            assistance=(
                None
                if self.assistance is None
                else OptionalAssistance(
                    offer=self.assistance.offer,
                    follow_up_queries=tuple(self.assistance.follow_up_queries),
                )
            ),
        )


class QueryPlanner:
    def __init__(self, provider: StructuredChatProvider) -> None:
        self._provider = provider

    async def plan(self, request: str) -> PlanningDecision:
        _reject_forbidden_request(request)
        result = await self._provider.generate_structured(
            messages=(
                ProviderMessage(role="system", content=PLANNING_SYSTEM_PROMPT),
                ProviderMessage(role="user", content=request),
            ),
            response_model=PlanningDraft,
            temperature=0.0,
        )
        draft = PlanningDraft.model_validate(result.response.model_dump(mode="python"))
        decision = draft.to_decision()
        _validate_scoped_queries(request=request, decision=decision)
        return decision


class AssistancePolicy:
    @staticmethod
    def validate(
        *,
        answer_completed: bool,
        request: str,
        assistance: OptionalAssistance | None,
    ) -> OptionalAssistance | None:
        if assistance is None:
            return None
        if not answer_completed:
            msg = "assistance requires the requested answer first"
            raise PlanningPolicyError(msg)
        request_tokens = _meaningful_tokens(request)
        for follow_up_query in assistance.follow_up_queries:
            if not request_tokens.intersection(_meaningful_tokens(follow_up_query)):
                msg = "assistance follow-up queries must stay tied to the answered request"
                raise PlanningPolicyError(msg)
        return assistance


def _reject_forbidden_request(request: str) -> None:
    lowered_request = request.casefold()
    for marker in _FORBIDDEN_REQUEST_MARKERS:
        if marker in lowered_request:
            raise PlanningPolicyError(f"request asks for a prohibited capability: {marker}")


def _validate_scoped_queries(*, request: str, decision: PlanningDecision) -> None:
    if decision.query_plan is None:
        return
    request_tokens = _meaningful_tokens(request)
    for search in decision.query_plan.searches:
        if not request_tokens.intersection(_meaningful_tokens(search.text)):
            msg = "search queries must stay scoped to the user request"
            raise PlanningPolicyError(msg)


def _meaningful_tokens(text: str) -> set[str]:
    return set(_TOKEN_PATTERN.findall(text.casefold()))
