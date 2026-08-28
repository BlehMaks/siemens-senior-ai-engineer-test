"""Bounded, review-gated memory views for optional answer synthesis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import Field, ValidationError, model_validator

from ..contracts import OpaqueId, StrictModel
from .procedural import (
    ProcedureRepository,
    ProcedureReviewState,
    ProcedureVersion,
)
from .semantic import (
    FactReviewState,
    SemanticFact,
    SemanticFactRepository,
)

MAX_CONTEXT_FACTS = 8
MAX_CONTEXT_PROCEDURES = 4
_MAX_CONTEXT_BYTES = 32 * 1024


class ReviewedMemoryContext(StrictModel):
    """Validated records that may be serialized only as untrusted user data."""

    tenant_id: OpaqueId
    observed_at: datetime
    facts: tuple[SemanticFact, ...] = Field(max_length=MAX_CONTEXT_FACTS)
    procedures: tuple[ProcedureVersion, ...] = Field(
        max_length=MAX_CONTEXT_PROCEDURES
    )

    @model_validator(mode="after")
    def validate_reviewed_records(self) -> ReviewedMemoryContext:
        if (
            type(self.facts) is not tuple
            or type(self.procedures) is not tuple
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("reviewed memory containers are invalid")
        observed_at = self.observed_at.astimezone(UTC)
        if self.observed_at != observed_at:
            raise ValueError("reviewed memory timestamp must be UTC")
        if any(type(fact) is not SemanticFact for fact in self.facts) or any(
            type(procedure) is not ProcedureVersion
            for procedure in self.procedures
        ):
            raise ValueError("reviewed memory records are invalid")
        try:
            checked_facts = tuple(
                SemanticFact.model_validate(
                    fact.model_dump(mode="python", warnings="error"), strict=True
                )
                for fact in self.facts
            )
            checked_procedures = tuple(
                ProcedureVersion.model_validate(
                    procedure.model_dump(mode="python", warnings="error"),
                    strict=True,
                )
                for procedure in self.procedures
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise ValueError("reviewed memory records are invalid") from None
        if checked_facts != self.facts or checked_procedures != self.procedures:
            raise ValueError("reviewed memory records are invalid")
        if any(
            fact.tenant_id != self.tenant_id
            or fact.state is not FactReviewState.APPROVED
            or fact.review is None
            or fact.review.reviewed_at > observed_at
            or fact.expires_at <= observed_at
            for fact in self.facts
        ):
            raise ValueError("semantic memory is not active and reviewed")
        if any(
            procedure.tenant_id != self.tenant_id
            or procedure.state is not ProcedureReviewState.APPROVED
            or procedure.review is None
            or procedure.review.reviewed_at > observed_at
            for procedure in self.procedures
        ):
            raise ValueError("procedural memory is not active and reviewed")
        if len({fact.fact_id for fact in self.facts}) != len(self.facts) or len(
            {fact.conflict_key for fact in self.facts}
        ) != len(self.facts):
            raise ValueError("semantic memory contains duplicate identities")
        if len({item.procedure_id for item in self.procedures}) != len(
            self.procedures
        ):
            raise ValueError("procedural memory contains duplicate identities")
        if len(self.model_dump_json().encode("utf-8")) > _MAX_CONTEXT_BYTES:
            raise ValueError("reviewed memory context exceeds its byte limit")
        return self

    def to_untrusted_payload(self) -> dict[str, object]:
        return {
            "semantic_facts": [
                {
                    "fact_id": fact.fact_id,
                    "claim": fact.claim,
                    "source_id": fact.source_id,
                    "evidence_id": fact.evidence_id,
                    "source_url": str(fact.source_url),
                    "expires_at": fact.expires_at.isoformat(),
                }
                for fact in self.facts
            ],
            "active_procedures": [
                {
                    "procedure_id": procedure.procedure_id,
                    "version": procedure.version,
                    "title": procedure.title,
                    "steps": list(procedure.steps),
                }
                for procedure in self.procedures
            ],
        }


class ReviewedMemoryReadPort(Protocol):
    async def read_active(
        self, *, tenant_id: OpaqueId, at: datetime
    ) -> ReviewedMemoryContext: ...


@dataclass(frozen=True, slots=True)
class RepositoryReviewedMemoryReader:
    """Read-only adapter; it has no proposal, review, or activation methods."""

    semantic_facts: SemanticFactRepository
    procedures: ProcedureRepository

    async def read_active(
        self, *, tenant_id: OpaqueId, at: datetime
    ) -> ReviewedMemoryContext:
        return ReviewedMemoryContext(
            tenant_id=tenant_id,
            observed_at=at,
            facts=self.semantic_facts.list_active(
                tenant_id=tenant_id,
                at=at,
                limit=MAX_CONTEXT_FACTS,
            ),
            procedures=self.procedures.list_active(
                tenant_id=tenant_id,
                limit=MAX_CONTEXT_PROCEDURES,
            ),
        )


__all__ = [
    "MAX_CONTEXT_FACTS",
    "MAX_CONTEXT_PROCEDURES",
    "RepositoryReviewedMemoryReader",
    "ReviewedMemoryContext",
    "ReviewedMemoryReadPort",
]
