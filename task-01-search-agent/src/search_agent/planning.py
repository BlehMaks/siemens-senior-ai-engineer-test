from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from .contracts import (
    OptionalAssistance,
    QueryPlan,
    SearchQuery,
    StrictModel,
    ToolBudget,
)
from .providers import ProviderMessage, ProviderResponseError, StructuredChatProvider

PlanningText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=400, strip_whitespace=True),
]

_TOKEN_PATTERN = re.compile(r"[^\W_]{4,}", flags=re.UNICODE)
_SCOPE_GENERIC_TOKENS = {
    "com",
    "current",
    "find",
    "http",
    "https",
    "latest",
    "please",
    "search",
    "siemens",
    "www",
}
_FORBIDDEN_REQUEST_MARKERS = (
    "system prompt",
    "secret",
    "api key",
    "access token",
    "credential",
    "password",
    "private key",
    "developer message",
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
        if self.requires_search and (
            self.query_plan is None or not self.query_plan.searches
        ):
            msg = "search-required plans must include at least one search"
            raise ValueError(msg)
        if not self.requires_search and self.query_plan is not None:
            msg = "direct or clarification plans cannot include a query_plan"
            raise ValueError(msg)
        if self.requires_search != (
            self.task_category is TaskCategory.COMPANY_RESEARCH
        ):
            msg = "task category must agree with the search requirement"
            raise ValueError(msg)
        return self


class DraftSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: PlanningText
    max_results: StrictInt = Field(ge=1, le=5)


class DraftToolBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_search_queries: StrictInt = Field(ge=1, le=8)
    max_fetches: StrictInt = Field(ge=1, le=24)


class DraftQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_budget: DraftToolBudget
    searches: list[DraftSearchQuery] = Field(min_length=1, max_length=8)


class DraftOptionalAssistance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    offer: PlanningText
    follow_up_queries: list[PlanningText] = Field(default_factory=list, max_length=3)


class PlanningDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_category: TaskCategory
    requires_search: StrictBool
    answer_focus: PlanningText
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
        try:
            decision = draft.to_decision()
        except ValidationError as exc:
            raise ProviderResponseError(
                "planner output violated the public planning contract"
            ) from exc
        _validate_generated_policy(request=request, decision=decision)
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
        _reject_forbidden_request(assistance.offer)
        if not _stays_scoped(request=request, candidate=assistance.offer):
            msg = "assistance offer must stay tied to the answered request"
            raise PlanningPolicyError(msg)
        for follow_up_query in assistance.follow_up_queries:
            _reject_forbidden_request(follow_up_query)
            if not _stays_scoped(
                request=request,
                candidate=follow_up_query,
                min_shared_tokens=2,
            ):
                msg = "assistance follow-up queries must stay tied to the answered request"
                raise PlanningPolicyError(msg)
        return assistance


def _reject_forbidden_request(request: str) -> None:
    lowered_request = request.casefold()
    for marker in _FORBIDDEN_REQUEST_MARKERS:
        if marker in lowered_request:
            raise PlanningPolicyError(
                f"request asks for a prohibited capability: {marker}"
            )


def _validate_generated_policy(*, request: str, decision: PlanningDecision) -> None:
    _reject_forbidden_request(decision.answer_focus)
    if not _stays_scoped(request=request, candidate=decision.answer_focus):
        raise PlanningPolicyError("answer focus must stay scoped to the user request")
    if decision.query_plan is not None:
        for search in decision.query_plan.searches:
            _reject_forbidden_request(search.text)
            if not _stays_scoped(
                request=request, candidate=search.text, min_shared_tokens=2
            ):
                raise PlanningPolicyError(
                    "search queries must stay scoped to the user request"
                )
    if decision.assistance is not None:
        _reject_forbidden_request(decision.assistance.offer)
        if not _stays_scoped(request=request, candidate=decision.assistance.offer):
            raise PlanningPolicyError(
                "assistance offer must stay tied to the user request"
            )
        for query in decision.assistance.follow_up_queries:
            _reject_forbidden_request(query)
            if not _stays_scoped(request=request, candidate=query, min_shared_tokens=2):
                raise PlanningPolicyError(
                    "assistance queries must stay tied to the user request"
                )


def _stays_scoped(*, request: str, candidate: str, min_shared_tokens: int = 1) -> bool:
    normalized_request = " ".join(request.casefold().split())
    normalized_candidate = " ".join(candidate.casefold().split())
    if normalized_request == normalized_candidate:
        return True
    # Search actions need more evidence of scope than prose-only offers. Capping the
    # requirement by both token sets keeps exact single-subject requests usable.
    request_tokens = _meaningful_tokens(request)
    candidate_tokens = _meaningful_tokens(candidate)
    required_shared = min(min_shared_tokens, len(request_tokens), len(candidate_tokens))
    if required_shared == 0:
        return False
    return len(request_tokens.intersection(candidate_tokens)) >= required_shared


def _meaningful_tokens(text: str) -> set[str]:
    return set(_TOKEN_PATTERN.findall(text.casefold())) - _SCOPE_GENERIC_TOKENS
