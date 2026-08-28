"""Bounded episodic-memory contracts owned by Task 1."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import AnyHttpUrl, Field, StringConstraints, model_validator

from ..contracts import EventType, OpaqueId, StrictModel, TerminalState

ReflectionText = Annotated[
    str, StringConstraints(min_length=1, max_length=240, strip_whitespace=True)
]

# Unicode Default_Ignorable ranges include marks outside category Cf. Treating the
# complete set as sensitive prevents invisible characters from splitting secrets.
_DEFAULT_IGNORABLE_PATTERN = re.compile(
    "[\u00ad\u034f\u061c\u115f-\u1160\u17b4-\u17b5\u180b-\u180f"
    "\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f"
    "\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3"
    "\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
# DNS canonicalization removes token case, and an attacker can prefix a credential
# inside one label. Search the entire hostname without broadening public path topics.
_HOST_ADMIN_TOKEN_PATTERN = re.compile(r"(?i)sk-admin-[a-z0-9_-]{20,}")
_CONTROL_INSTRUCTION_PATTERN = re.compile(
    r"(?i)\b(?:forget|ignore|disregard|override)\s+"
    r"(?:(?:any|all|every|the)\s+)?"
    r"(?:previous|prior|system|developer)\s+(?:instructions?|rules?|prompts?)\b|"
    r"\b(?:grant|enable|allow)\s+(?:me\s+|yourself\s+|itself\s+|"
    r"themselves\s+|the\s+agent\s+|it\s+)?"
    r"(?:admin|browser|code|network|system|tool)\s+"
    r"(?:access|capabilit(?:y|ies)|permissions?)\b"
    r"|\b(?:replace|rewrite|change)\s+(?:the\s+)?"
    r"(?:system|developer)\s+(?:instructions?|rules?|prompts?)\b"
    r"|\b(?:execute|run|evaluate)\s+__import__\s*\("
    r"|\b__import__\s*\([^\n]{0,120}\)\.system\s*\("
)
_REDACTIONS = (
    re.compile(r"(?i)\bbearer\b[^.!?]*"),
    re.compile(
        r"(?i)\b(?:api[\s_-]?key|access[\s_-]?token|authorization|credential|"
        r"password|secret|token)\b\s*(?::|=|\bis\b)\s*(?:basic\s+)?[^.!?]*"
    ),
    re.compile(r"(?i)\bhttps?://[^/\s:@]+:[^@/\s]+@[^\s]*"),
    # Keep provider prefixes explicit: credential families stay blocked without
    # treating every hyphenated public slug as a secret. The short documented admin
    # example has an exact opaque shape. Full forms require mixed-case opaque data,
    # keeping arbitrarily long lowercase public topic slugs outside the boundary.
    re.compile(
        r"(?<![A-Za-z0-9])(?:"
        r"sk-(?:[A-Za-z0-9]{20,}|(?:proj|svcacct)-[A-Za-z0-9_-]{20,}|"
        r"admin-(?:[0-9]{4}[A-Za-z]{4}|"
        r"(?=[A-Za-z0-9_-]{20,}(?![A-Za-z0-9]))"
        r"(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{20,}))|"
        r"gh[pousr]_[A-Za-z0-9]{8,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|"
        r"xox[baprs]-[A-Za-z0-9-]{20,}|"
        r"xapp-[A-Za-z0-9-]{20,}|"
        r"AIza[A-Za-z0-9_-]{20,}|"
        r"ya29\.[A-Za-z0-9._-]{20,}|"
        r"(?:ABIA|AKIA|ASIA)[0-9A-Z]{16}|"
        r"(?:glpat|gloas|gldt|glrtr?|glcbt|glptt|glft|glimt|glagent|glwt|"
        r"glsoat|glffct)-[A-Za-z0-9_-]{20,}|"
        r"_gitlab_session=[A-Za-z0-9%_-]{20,}|"
        r"hf_[A-Za-z0-9]{20,}"
        r")(?![A-Za-z0-9])"
    ),
    re.compile(r"(?i)\b(?:system|hidden)\s+prompt\b(?:\s*(?::|=|\bis\b)\s*)?[^.!?]*"),
    re.compile(
        r"(?i)\b(?:raw\s+page|chain\s+of\s+thought|model\s+reasoning)\b"
        r"(?:\s*(?::|=|\bis\b)\s*)?[^.!?]*"
    ),
    re.compile(
        r"(?i)\b(?:[a-z0-9]+[-_])?private[-_]"
        r"(?:sentinel|detail|tail)(?:[-_][a-z0-9]+)*\b"
    ),
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:Error|Exception)\b(?::[^.!?]*)?"),
)


class FailureCode(StrEnum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    NO_EVIDENCE = "no_evidence"
    SEARCH_FAILED = "search_failed"
    VALIDATION_FAILED = "validation_failed"
    PAGE_PROCESSING_FAILED = "page_processing_failed"


class RecoveryStep(StrEnum):
    CONTINUED_WITH_REMAINING_EVIDENCE = "continued_with_remaining_evidence"


class UnresolvedItem(StrEnum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    NO_EVIDENCE = "no_evidence"
    SEARCH_FAILED = "search_failed"
    VALIDATION_FAILED = "validation_failed"


class ObservedFailure(StrictModel):
    code: FailureCode
    count: int = Field(ge=1, le=24)


class CompletionEvidence(StrictModel):
    evidence_id: str = Field(
        min_length=3,
        max_length=32,
        pattern=r"^ev-[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    source_url: AnyHttpUrl


class ReflectionUsage(StrictModel):
    elapsed_seconds: float = Field(ge=0.0, le=600.0)
    iterations: int = Field(ge=0, le=256)
    search_queries: int = Field(ge=0, le=8)
    pages: int = Field(ge=0, le=24)
    failed_pages: int = Field(ge=0, le=24)
    raw_bytes_reserved: int = Field(ge=0, le=128 * 1024 * 1024)
    decoded_bytes: int = Field(ge=0, le=128 * 1024 * 1024)
    model_calls: int = Field(ge=0, le=16)
    model_attempts: int = Field(ge=0, le=96)
    tokens: int = Field(ge=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_related_counts(self) -> ReflectionUsage:
        if self.failed_pages > self.pages:
            raise ValueError("failed pages cannot exceed attempted pages")
        if self.model_attempts > self.model_calls * 6:
            raise ValueError("model attempts exceed the provider retry ceiling")
        return self


class RunReflection(StrictModel):
    schema_version: Literal[1] = 1
    tenant_id: OpaqueId
    session_id: OpaqueId
    run_id: OpaqueId
    requested_outcome: ReflectionText
    actions: tuple[EventType, ...] = Field(max_length=16)
    failures: tuple[ObservedFailure, ...] = Field(max_length=5)
    recovery_steps: tuple[RecoveryStep, ...] = Field(max_length=4)
    completion_evidence: tuple[CompletionEvidence, ...] = Field(max_length=16)
    unresolved_items: tuple[UnresolvedItem, ...] = Field(max_length=4)
    outcome: TerminalState
    usage: ReflectionUsage

    @model_validator(mode="after")
    def validate_outcome_and_text(self) -> RunReflection:
        if contains_sensitive_memory_text(self.requested_outcome):
            raise ValueError("requested outcome contains sensitive material")
        if self.outcome is TerminalState.COMPLETED:
            evidence_required = EventType.EVIDENCE_READY in self.actions
            if evidence_required != bool(self.completion_evidence):
                raise ValueError("completed reflections require resolved evidence")
            if self.unresolved_items:
                raise ValueError("completed reflections cannot be unresolved")
        elif self.completion_evidence:
            raise ValueError(
                "non-completed reflections cannot claim completion evidence"
            )
        if self.outcome is TerminalState.CANCELLED and self.unresolved_items != (
            UnresolvedItem.CANCELLED,
        ):
            raise ValueError("cancelled reflections require one cancelled item")
        if self.outcome is TerminalState.FAILED and not self.unresolved_items:
            raise ValueError("failed reflections require an unresolved item")
        return self


class ReflectionRepository(Protocol):
    """Exact-scope persistence contract; a matching key is replaced deterministically."""

    def put(self, reflection: RunReflection) -> None: ...

    def get(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, run_id: OpaqueId
    ) -> RunReflection | None: ...

    def list_session(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, limit: int = 100
    ) -> tuple[RunReflection, ...]: ...

    def delete_run(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, run_id: OpaqueId
    ) -> bool: ...

    def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int: ...

    def delete_tenant(self, *, tenant_id: OpaqueId) -> int: ...


class ReflectionInputError(ValueError):
    """A typed rejection without echoing untrusted public content."""


class ReflectionStorageError(RuntimeError):
    """A safe storage failure without row or database detail."""


class RepositoryClosedError(ReflectionStorageError):
    pass


def redact_memory_text(value: str) -> str:
    """Redact known credential, prompt, page, reasoning, and exception shapes."""

    if type(value) is not str:
        raise ReflectionInputError("memory text must be a string")
    redacted = _DEFAULT_IGNORABLE_PATTERN.sub("", " ".join(value.split()))
    for pattern in _REDACTIONS:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = " ".join(redacted.split()).strip()
    if not redacted:
        redacted = "[REDACTED]"
    return redacted[:240].rstrip()


def contains_sensitive_memory_text(value: str) -> bool:
    return bool(_DEFAULT_IGNORABLE_PATTERN.search(value)) or any(
        pattern.search(value) for pattern in _REDACTIONS
    )


def contains_memory_control_text(value: str) -> bool:
    """Detect imperative prompt or capability changes that memory must never carry."""

    return type(value) is not str or bool(_CONTROL_INSTRUCTION_PATTERN.search(value))


def contains_sensitive_memory_hostname(value: str) -> bool:
    """Detect credentials after case-insensitive DNS canonicalization."""

    return contains_sensitive_memory_text(value) or bool(
        _HOST_ADMIN_TOKEN_PATTERN.search(value)
    )
