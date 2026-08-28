"""Tenant-scoped episodic reflections and repository adapters."""

from .contracts import (
    CompletionEvidence,
    FailureCode,
    ObservedFailure,
    RecoveryStep,
    ReflectionInputError,
    ReflectionRepository,
    ReflectionStorageError,
    ReflectionUsage,
    RepositoryClosedError,
    RunReflection,
    UnresolvedItem,
)
from .episodic import (
    InMemoryReflectionRepository,
    SQLiteReflectionRepository,
    reflect_run,
)
from .semantic import (
    FactAuthor,
    FactConflictError,
    FactReview,
    FactReviewState,
    InMemorySemanticFactRepository,
    SemanticFact,
    SemanticFactRepository,
    SQLiteSemanticFactRepository,
)

__all__ = [
    "CompletionEvidence",
    "FactAuthor",
    "FactConflictError",
    "FactReview",
    "FactReviewState",
    "FailureCode",
    "InMemoryReflectionRepository",
    "InMemorySemanticFactRepository",
    "ObservedFailure",
    "RecoveryStep",
    "ReflectionInputError",
    "ReflectionRepository",
    "ReflectionStorageError",
    "ReflectionUsage",
    "RepositoryClosedError",
    "RunReflection",
    "SQLiteReflectionRepository",
    "SQLiteSemanticFactRepository",
    "SemanticFact",
    "SemanticFactRepository",
    "UnresolvedItem",
    "reflect_run",
]
