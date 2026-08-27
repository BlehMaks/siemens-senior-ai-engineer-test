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

__all__ = [
    "CompletionEvidence",
    "FailureCode",
    "InMemoryReflectionRepository",
    "ObservedFailure",
    "RecoveryStep",
    "ReflectionInputError",
    "ReflectionRepository",
    "ReflectionStorageError",
    "ReflectionUsage",
    "RepositoryClosedError",
    "RunReflection",
    "SQLiteReflectionRepository",
    "UnresolvedItem",
    "reflect_run",
]
