"""Bounded search, retrieval, and local extraction adapters."""

from .extract import (
    AsyncLocalExtractor,
    ExtractedBlock,
    ExtractedDocument,
    ExtractionError,
    ExtractionFailureReason,
    LocalExtractor,
)
from .fetch import (
    FetchedDocument,
    FetchError,
    FetchFailureReason,
    GuardedFetcher,
    create_fetch_client,
)
from .search import (
    SearchAdapter,
    SearchAttempt,
    SearchAttemptOutcome,
    SearchFailure,
    SearchResult,
    SyncSearchBackend,
)

__all__ = [
    "AsyncLocalExtractor",
    "ExtractedBlock",
    "ExtractedDocument",
    "ExtractionError",
    "ExtractionFailureReason",
    "FetchError",
    "FetchFailureReason",
    "FetchedDocument",
    "GuardedFetcher",
    "LocalExtractor",
    "SearchAdapter",
    "SearchAttempt",
    "SearchAttemptOutcome",
    "SearchFailure",
    "SearchResult",
    "SyncSearchBackend",
    "create_fetch_client",
]
