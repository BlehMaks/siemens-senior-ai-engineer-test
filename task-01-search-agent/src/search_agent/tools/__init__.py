"""Bounded search, retrieval, and local extraction adapters."""

from .extract import (
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
from .search import SearchAdapter, SearchFailure, SyncSearchBackend

__all__ = [
    "ExtractedDocument",
    "ExtractionError",
    "ExtractionFailureReason",
    "FetchError",
    "FetchFailureReason",
    "FetchedDocument",
    "GuardedFetcher",
    "LocalExtractor",
    "SearchAdapter",
    "SearchFailure",
    "SyncSearchBackend",
    "create_fetch_client",
]
