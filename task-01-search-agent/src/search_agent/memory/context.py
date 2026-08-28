"""Bounded, review-gated memory views for optional answer synthesis."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Protocol

from pydantic import Field, ValidationError, field_validator, model_validator

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


class ActiveProcedure(StrictModel):
    """A procedure version selected through the repository's active pointer."""

    active_version: int = Field(ge=1, le=10_000)
    procedure: ProcedureVersion

    @model_validator(mode="after")
    def validate_active_selection(self) -> ActiveProcedure:
        if type(self.procedure) is not ProcedureVersion:
            raise ValueError("active procedure record is invalid")
        try:
            checked = ProcedureVersion.model_validate(
                self.procedure.model_dump(mode="python", warnings="error"),
                strict=True,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise ValueError("active procedure record is invalid") from None
        if checked != self.procedure or self.active_version != checked.version:
            raise ValueError("active procedure selection is inconsistent")
        return self


class ReviewedMemoryContext(StrictModel):
    """Validated records that may be serialized only as untrusted user data."""

    tenant_id: OpaqueId
    observed_at: datetime
    facts: tuple[SemanticFact, ...] = Field(max_length=MAX_CONTEXT_FACTS)
    procedures: tuple[ActiveProcedure, ...] = Field(
        max_length=MAX_CONTEXT_PROCEDURES
    )

    @field_validator("procedures", mode="before")
    @classmethod
    def require_active_selections(cls, value: object) -> object:
        if type(value) is not tuple or any(
            type(item) is not ActiveProcedure for item in value
        ):
            raise ValueError("procedural memory requires active selections")
        return value

    @model_validator(mode="after")
    def validate_reviewed_records(self) -> ReviewedMemoryContext:
        if (
            type(self.facts) is not tuple
            or type(self.procedures) is not tuple
        ):
            raise ValueError("reviewed memory containers are invalid")
        if (
            self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("reviewed memory timestamp must be UTC")
        observed_at = self.observed_at.astimezone(UTC)
        if any(type(fact) is not SemanticFact for fact in self.facts) or any(
            type(procedure) is not ActiveProcedure
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
                ActiveProcedure.model_validate(
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
            selection.procedure.tenant_id != self.tenant_id
            or selection.procedure.state is not ProcedureReviewState.APPROVED
            or selection.procedure.review is None
            or selection.procedure.review.reviewed_at > observed_at
            for selection in self.procedures
        ):
            raise ValueError("procedural memory is not active and reviewed")
        if len({fact.fact_id for fact in self.facts}) != len(self.facts) or len(
            {fact.conflict_key for fact in self.facts}
        ) != len(self.facts):
            raise ValueError("semantic memory contains duplicate identities")
        if len({item.procedure.procedure_id for item in self.procedures}) != len(
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
                    "procedure_id": selection.procedure.procedure_id,
                    "version": selection.procedure.version,
                    "title": selection.procedure.title,
                    "steps": list(selection.procedure.steps),
                }
                for selection in self.procedures
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
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    async def read_active(
        self, *, tenant_id: OpaqueId, at: datetime
    ) -> ReviewedMemoryContext:
        return await asyncio.to_thread(self._read_active, tenant_id, at)

    def _read_active(
        self, tenant_id: OpaqueId, at: datetime
    ) -> ReviewedMemoryContext:
        with self._lock:
            facts = self.semantic_facts.list_active(
                tenant_id=tenant_id,
                at=at,
                limit=MAX_CONTEXT_FACTS,
            )
            procedures = self.procedures.list_active(
                tenant_id=tenant_id,
                limit=MAX_CONTEXT_PROCEDURES,
            )
        return ReviewedMemoryContext(
            tenant_id=tenant_id,
            observed_at=at,
            facts=facts,
            procedures=tuple(
                ActiveProcedure(
                    active_version=procedure.version,
                    procedure=procedure,
                )
                for procedure in procedures
            ),
        )


__all__ = [
    "MAX_CONTEXT_FACTS",
    "MAX_CONTEXT_PROCEDURES",
    "ActiveProcedure",
    "RepositoryReviewedMemoryReader",
    "ReviewedMemoryContext",
    "ReviewedMemoryReadPort",
]
