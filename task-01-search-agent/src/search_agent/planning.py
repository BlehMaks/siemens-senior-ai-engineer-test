from __future__ import annotations

import re
import unicodedata
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
    ScopedAnswer,
    SearchQuery,
    StrictModel,
    ToolBudget,
)
from .providers import (
    ProviderMessage,
    ProviderMetadata,
    ProviderResponseError,
    StructuredChatProvider,
)

PlanningText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=400, strip_whitespace=True),
]

_TOKEN_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)
_POLICY_WORD_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)
_ACRONYM_PATTERN = re.compile(r"\b[A-Z]{2,8}\b")
_YEAR_PATTERN = re.compile(r"(?:19|20)\d{2}\Z")
_CLAIM_SEGMENT_PATTERN = re.compile(r"(?:\n+|;+|(?<!\d)[.!?]+|[.!?]+(?!\d))")
_SCOPE_GENERIC_TOKENS = {
    "a",
    "an",
    "and",
    "about",
    "are",
    "as",
    "at",
    "be",
    "been",
    "but",
    "by",
    "compare",
    "com",
    "could",
    "current",
    "explain",
    "find",
    "for",
    "from",
    "has",
    "have",
    "how",
    "http",
    "https",
    "in",
    "into",
    "is",
    "it",
    "latest",
    "look",
    "more",
    "next",
    "of",
    "on",
    "or",
    "per",
    "please",
    "previous",
    "research",
    "review",
    "search",
    "summarize",
    "than",
    "that",
    "the",
    "this",
    "to",
    "via",
    "vs",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "www",
    "year",
}
_KNOWN_ACRONYM_EXPANSIONS = {
    "ai": ("artificial", "intelligence"),
    "ml": ("machine", "learning"),
}
_FORBIDDEN_REQUEST_MARKERS = (
    "system prompt",
    "system prompts",
    "secret",
    "secrets",
    "api key",
    "api keys",
    "access token",
    "access tokens",
    "credential",
    "credentials",
    "password",
    "passwords",
    "private key",
    "private keys",
    "developer message",
    "developer messages",
    "browser",
    "browsers",
    "playwright",
    "shell",
    "shells",
    "terminal",
    "terminals",
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


class PlanningOutcome(StrictModel):
    decision: PlanningDecision
    metadata: ProviderMetadata


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
        return (await self.plan_with_metadata(request)).decision

    async def plan_with_metadata(self, request: str) -> PlanningOutcome:
        _reject_forbidden_request(request)
        result = await self._provider.generate_structured(
            messages=(
                ProviderMessage(role="system", content=PLANNING_SYSTEM_PROMPT),
                ProviderMessage(role="user", content=request),
            ),
            response_model=PlanningDraft,
            temperature=0.0,
        )
        try:
            draft = PlanningDraft.model_validate(
                result.response.model_dump(mode="python")
            )
            decision = draft.to_decision()
        except ValidationError as exc:
            raise ProviderResponseError(
                "planner output violated the public planning contract"
            ) from exc
        validate_planning_decision(request=request, decision=decision)
        if decision.task_category is TaskCategory.CLARIFICATION:
            decision = PlanningDecision(
                task_category=TaskCategory.CLARIFICATION,
                requires_search=False,
                answer_focus="Ask the user to clarify the original request.",
            )
        return PlanningOutcome(decision=decision, metadata=result.metadata)


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
        if not _stays_scoped(
            request=request,
            candidate=assistance.offer,
            restrict_expansions=True,
        ):
            msg = "assistance offer must stay tied to the answered request"
            raise PlanningPolicyError(msg)
        for follow_up_query in assistance.follow_up_queries:
            _reject_forbidden_request(follow_up_query)
            if not _stays_scoped(
                request=request,
                candidate=follow_up_query,
                min_shared_tokens=2,
                restrict_expansions=True,
            ):
                msg = "assistance follow-up queries must stay tied to the answered request"
                raise PlanningPolicyError(msg)
        return assistance


class AnswerScopePolicy:
    """Reject cited output that strays from the validated research intent."""

    _INSTRUCTION_MARKERS = (
        "follow these instructions",
        "ignore previous instructions",
        "ignore prior instructions",
        "reveal the prompt",
        "send money",
        "transfer funds",
    )

    @classmethod
    def validate(
        cls,
        *,
        request: str,
        answer_focus: str,
        answer: ScopedAnswer,
    ) -> ScopedAnswer:
        if not _stays_scoped(
            request=request,
            candidate=answer_focus,
            restrict_expansions=True,
        ):
            raise PlanningPolicyError("answer focus must stay scoped to the request")
        _reject_forbidden_request(answer.answer_text)
        normalized_answer = _normalized_policy_text(answer.answer_text)
        if any(marker in normalized_answer for marker in cls._INSTRUCTION_MARKERS):
            raise PlanningPolicyError("answer contains unrequested instructions")
        for citation in answer.citations:
            for segment in _CLAIM_SEGMENT_PATTERN.split(citation.claim):
                if not segment.strip():
                    continue
                if not (
                    _stays_scoped(
                        request=request,
                        candidate=segment,
                        min_shared_tokens=2,
                    )
                    or _stays_scoped(
                        request=answer_focus,
                        candidate=segment,
                        min_shared_tokens=2,
                    )
                ):
                    raise PlanningPolicyError(
                        "answer claim must stay scoped to the research request"
                    )
        return answer


def validate_planning_decision(
    *, request: str, decision: PlanningDecision
) -> PlanningDecision:
    """Re-check an injected planning port before any tool can run."""

    _reject_forbidden_request(request)
    _validate_generated_policy(request=request, decision=decision)
    return decision


def _reject_forbidden_request(request: str) -> None:
    normalized_request = f" {_normalized_policy_text(request)} "
    for marker in _FORBIDDEN_REQUEST_MARKERS:
        normalized_marker = _normalized_policy_text(marker)
        if f" {normalized_marker} " in normalized_request:
            raise PlanningPolicyError(
                f"request asks for a prohibited capability: {marker}"
            )


def _validate_generated_policy(*, request: str, decision: PlanningDecision) -> None:
    _reject_forbidden_request(decision.answer_focus)
    if decision.task_category is TaskCategory.CLARIFICATION:
        # Clarification text is replaced with a fixed prompt below. Scan every
        # discarded field for prohibited content, but do not assign it scope.
        if decision.assistance is not None:
            _reject_forbidden_request(decision.assistance.offer)
            for query in decision.assistance.follow_up_queries:
                _reject_forbidden_request(query)
        return
    company_focus_is_invalid = (
        decision.task_category is TaskCategory.COMPANY_RESEARCH
        and not _stays_scoped(
            request=request,
            candidate=decision.answer_focus,
            restrict_expansions=True,
        )
    )
    direct_focus_is_invalid = (
        decision.task_category is TaskCategory.DIRECT_REPLY
        and not _stays_scoped(
            request=request,
            candidate=decision.answer_focus,
            restrict_expansions=True,
        )
        and not _expands_request_acronym(
            request=request, candidate=decision.answer_focus
        )
    )
    if company_focus_is_invalid or direct_focus_is_invalid:
        raise PlanningPolicyError("answer focus must stay scoped to the user request")
    if decision.query_plan is not None:
        for search in decision.query_plan.searches:
            _reject_forbidden_request(search.text)
            if not _stays_scoped(
                request=request,
                candidate=search.text,
                min_shared_tokens=2,
                restrict_expansions=True,
            ):
                raise PlanningPolicyError(
                    "search queries must stay scoped to the user request"
                )
    if decision.assistance is not None:
        _reject_forbidden_request(decision.assistance.offer)
        if not _stays_scoped(
            request=request,
            candidate=decision.assistance.offer,
            restrict_expansions=True,
        ):
            raise PlanningPolicyError(
                "assistance offer must stay tied to the user request"
            )
        for query in decision.assistance.follow_up_queries:
            _reject_forbidden_request(query)
            if not _stays_scoped(
                request=request,
                candidate=query,
                min_shared_tokens=2,
                restrict_expansions=True,
            ):
                raise PlanningPolicyError(
                    "assistance queries must stay tied to the user request"
                )


def _stays_scoped(
    *,
    request: str,
    candidate: str,
    min_shared_tokens: int = 1,
    restrict_expansions: bool = False,
) -> bool:
    normalized_request = " ".join(request.casefold().split())
    normalized_candidate = " ".join(candidate.casefold().split())
    if normalized_request == normalized_candidate:
        return True
    # Generated actions may narrow the request by year, but cannot invent another
    # topic. This deterministic rule is stricter than semantic guessing by design.
    request_tokens = _meaningful_tokens(request)
    candidate_tokens = _meaningful_tokens(candidate)
    candidate_topic_tokens = {
        token for token in candidate_tokens if not _YEAR_PATTERN.fullmatch(token)
    }
    required_shared = min(
        min_shared_tokens,
        len(request_tokens),
        len(candidate_topic_tokens),
    )
    if required_shared == 0:
        return False
    shared_count = len(request_tokens.intersection(candidate_tokens))
    added_tokens = candidate_tokens - request_tokens
    return shared_count >= required_shared and (
        not restrict_expansions
        or all(_YEAR_PATTERN.fullmatch(token) for token in added_tokens)
    )


def _meaningful_tokens(text: str) -> set[str]:
    return set(_TOKEN_PATTERN.findall(text.casefold())) - _SCOPE_GENERIC_TOKENS


def _normalized_policy_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_POLICY_WORD_PATTERN.findall(normalized))


def _expands_request_acronym(*, request: str, candidate: str) -> bool:
    candidate_tokens = tuple(_TOKEN_PATTERN.findall(candidate.casefold()))
    request_acronyms = {
        acronym.casefold() for acronym in _ACRONYM_PATTERN.findall(request)
    }
    # A closed vocabulary is safer than accepting arbitrary same-initial phrases.
    return any(
        candidate_tokens in {expansion, ("explain", *expansion)}
        for acronym, expansion in _KNOWN_ACRONYM_EXPANSIONS.items()
        if acronym in request_acronyms
    )
