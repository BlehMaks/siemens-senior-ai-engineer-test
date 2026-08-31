"""Bounded asynchronous adapter for the synchronous DDGS text API."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic_ns
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from search_agent.contracts import SearchHit, SearchQuery
from search_agent.security.site_policy import SafeSearch, SitePolicy

_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_ROWS_PER_REQUESTED_HIT = 4
_MAX_ATTEMPT_DURATION_MS = 600_000
_MAX_SEARCH_BACKENDS = 2
# Public search endpoints throttle bursts, and a throttled attempt raises rather
# than returning nothing. One bounded second pass turns that transient refusal
# into a result instead of ending the run without evidence.
_SEARCH_SWEEPS = 2
_SEARCH_RETRY_DELAY_SECONDS = 1.5
_MAX_SEARCH_BACKENDS_TEXT = 64
_SUPPORTED_SEARCH_BACKENDS = frozenset({"auto", "brave", "duckduckgo", "yahoo"})
_SAFE_SEARCH_ARGUMENT = {
    SafeSearch.STRICT: "on",
    SafeSearch.MODERATE: "moderate",
}


class SyncSearchBackend(Protocol):
    """The subset of ``DDGS`` used by the adapter."""

    def text(self, query: str, **kwargs: object) -> Sequence[object]: ...


class SearchFailure(RuntimeError):
    """A stable public failure that does not expose backend internals."""

    def __init__(
        self, message: str, *, attempts: tuple[SearchAttempt, ...] = ()
    ) -> None:
        super().__init__(message)
        self.attempts = attempts


class SearchAttemptOutcome(StrEnum):
    """Safe, bounded reason codes for one configured backend attempt."""

    EXCEPTION = "exception"
    RAW_EMPTY = "raw_empty"
    INVALID = "invalid"
    ALL_POLICY_REJECTED = "all_policy_rejected"
    NORMALIZED_EMPTY = "normalized_empty"
    SUCCESS = "success"


@dataclass(frozen=True, slots=True)
class SearchAttempt:
    backend: str
    outcome: SearchAttemptOutcome
    raw_rows: int = 0
    accepted_hits: int = 0
    rejection_count: int = 0
    duration_ms: int = 0

    @property
    def reason_code(self) -> str:
        """Return the bounded outcome value used by action tracing."""

        return self.outcome.value


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    attempts: tuple[SearchAttempt, ...]


@dataclass(frozen=True, slots=True)
class _NormalizationResult:
    hits: tuple[SearchHit, ...]
    raw_rows: int
    invalid_rows: int
    policy_rejected_rows: int
    duplicate_rows: int

    @property
    def rejection_count(self) -> int:
        return self.invalid_rows + self.policy_rejected_rows + self.duplicate_rows


def parse_search_backends(value: str) -> tuple[str, ...]:
    """Parse the bounded comma-separated CLI and environment contract."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_SEARCH_BACKENDS_TEXT
        or not value.isascii()
    ):
        raise ValueError("search backends must be short non-empty ASCII text")
    return validate_search_backends(tuple(value.split(",")))


def validate_search_backends(values: Sequence[str]) -> tuple[str, ...]:
    """Validate an ordered backend list without silently normalizing it."""

    if isinstance(values, (str, bytes)):
        raise ValueError("search backends must be an ordered sequence")
    backends = tuple(values)
    if not 1 <= len(backends) <= _MAX_SEARCH_BACKENDS:
        raise ValueError("search backends must contain one or two entries")
    if any(
        not isinstance(backend, str)
        or not backend
        or backend.strip() != backend
        or backend not in _SUPPORTED_SEARCH_BACKENDS
        for backend in backends
    ):
        raise ValueError("search backend is not allowed")
    if len(set(backends)) != len(backends):
        raise ValueError("search backends must not contain duplicates")
    return backends


@dataclass(frozen=True, slots=True)
class SearchAdapter:
    backend: SyncSearchBackend
    site_policy: SitePolicy
    region: str = "us-en"
    backend_names: tuple[str, ...] = ("auto",)
    backend_name: str | None = None
    # Exposed so a test can exercise the retry sweep without waiting for it.
    retry_delay_seconds: float = _SEARCH_RETRY_DELAY_SECONDS

    def __post_init__(self) -> None:
        names = self.backend_names
        if self.backend_name is not None:
            if names != ("auto",) and names != (self.backend_name,):
                raise ValueError("backend_name conflicts with backend_names")
            names = (self.backend_name,)
        if not isinstance(self.retry_delay_seconds, (int, float)) or not (
            0 <= self.retry_delay_seconds <= 10
        ):
            raise ValueError("retry_delay_seconds must be between 0 and 10")
        names = validate_search_backends(names)
        object.__setattr__(self, "backend_names", names)
        object.__setattr__(self, "backend_name", names[0])

    async def search(self, query: SearchQuery) -> tuple[SearchHit, ...]:
        """Run blocking DDGS work off-loop and return bounded, policy-safe hits."""

        return (await self.search_with_metadata(query)).hits

    async def search_with_metadata(self, query: SearchQuery) -> SearchResult:
        """Try configured backends in order and return safe attempt metadata."""

        attempts: list[SearchAttempt] = []
        for sweep in range(_SEARCH_SWEEPS):
            if sweep and self.retry_delay_seconds > 0:
                await asyncio.sleep(self.retry_delay_seconds)
            result = await self._sweep_backends(query, attempts)
            if result is not None:
                return result
            # Only a throttled or broken backend is worth a second sweep; a clean
            # empty result means the query genuinely found nothing.
            if not all(
                attempt.outcome
                in {SearchAttemptOutcome.EXCEPTION, SearchAttemptOutcome.INVALID}
                for attempt in attempts
            ):
                break

        attempt_tuple = tuple(attempts)
        if attempt_tuple and all(
            attempt.outcome
            in {SearchAttemptOutcome.EXCEPTION, SearchAttemptOutcome.INVALID}
            for attempt in attempt_tuple
        ):
            message = (
                "search backend returned an invalid result"
                if any(
                    attempt.outcome is SearchAttemptOutcome.INVALID
                    for attempt in attempt_tuple
                )
                else "search backend failed"
            )
            raise SearchFailure(message, attempts=attempt_tuple) from None
        return SearchResult(hits=(), attempts=attempt_tuple)

    async def _sweep_backends(
        self, query: SearchQuery, attempts: list[SearchAttempt]
    ) -> SearchResult | None:
        """Try every configured backend once, returning the first result with hits."""

        for backend_name in self.backend_names:
            started_ns = monotonic_ns()
            try:
                rows = await asyncio.to_thread(
                    self.backend.text,
                    query.text,
                    region=self.region,
                    safesearch=_SAFE_SEARCH_ARGUMENT[self.site_policy.safe_search],
                    max_results=query.max_results,
                    backend=backend_name,
                )
            except Exception:
                attempts.append(
                    SearchAttempt(
                        backend=backend_name,
                        outcome=SearchAttemptOutcome.EXCEPTION,
                        duration_ms=_duration_ms(started_ns),
                    )
                )
                continue

            if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
                attempts.append(
                    SearchAttempt(
                        backend=backend_name,
                        outcome=SearchAttemptOutcome.INVALID,
                        duration_ms=_duration_ms(started_ns),
                    )
                )
                continue
            try:
                normalized = self._normalize(rows, query.max_results)
            except Exception:
                attempts.append(
                    SearchAttempt(
                        backend=backend_name,
                        outcome=SearchAttemptOutcome.INVALID,
                        duration_ms=_duration_ms(started_ns),
                    )
                )
                continue

            outcome = _attempt_outcome(normalized)
            attempts.append(
                SearchAttempt(
                    backend=backend_name,
                    outcome=outcome,
                    raw_rows=normalized.raw_rows,
                    accepted_hits=len(normalized.hits),
                    rejection_count=normalized.rejection_count,
                    duration_ms=_duration_ms(started_ns),
                )
            )
            if normalized.hits:
                return SearchResult(
                    hits=normalized.hits,
                    attempts=tuple(attempts),
                )
        return None

    def _normalize(
        self, rows: Sequence[object], max_results: int
    ) -> _NormalizationResult:
        hits: list[SearchHit] = []
        seen_urls: set[str] = set()
        raw_rows = 0
        invalid_rows = 0
        policy_rejected_rows = 0
        duplicate_rows = 0

        # The backend was asked for max_results; a small allowance preserves useful
        # filtering without letting a hostile Sequence create unbounded work.
        iterator = iter(rows)
        for _ in range(max_results * _ROWS_PER_REQUESTED_HIT):
            try:
                row = next(iterator)
            except StopIteration:
                break
            raw_rows += 1
            normalized, reason = self._normalize_row(row, rank=len(hits) + 1)
            if normalized is None:
                if reason == "policy_rejected":
                    policy_rejected_rows += 1
                else:
                    invalid_rows += 1
                continue
            canonical_url = str(normalized.url)
            if canonical_url in seen_urls:
                duplicate_rows += 1
                continue
            seen_urls.add(canonical_url)
            hits.append(normalized)
            if len(hits) == max_results:
                break

        return _NormalizationResult(
            hits=tuple(hits),
            raw_rows=raw_rows,
            invalid_rows=invalid_rows,
            policy_rejected_rows=policy_rejected_rows,
            duplicate_rows=duplicate_rows,
        )

    def _normalize_row(
        self, row: object, *, rank: int
    ) -> tuple[SearchHit | None, str | None]:
        if not isinstance(row, Mapping):
            return None, "invalid"

        try:
            title = _normalize_text(row.get("title"))
            raw_href = row.get("href")
            snippet = _normalize_text(row.get("body"))
            if title is None or snippet is None or not isinstance(raw_href, str):
                return None, "invalid"
            href = raw_href.strip()
        except Exception:
            raise SearchFailure("search backend returned an invalid result") from None

        try:
            parsed_url = _URL_ADAPTER.validate_python(href)
        except ValidationError:
            return None, "invalid"
        if parsed_url.username is not None or parsed_url.password is not None:
            return None, "policy_rejected"
        if parsed_url.port not in self.site_policy.allowed_ports:
            return None, "policy_rejected"

        raw_host = parsed_url.host
        if raw_host is None:
            return None, "invalid"
        host = raw_host.strip("[]").rstrip(".")
        try:
            decision = self.site_policy.evaluate(host)
        except ValueError:
            return None, "invalid"
        if not decision.allowed:
            return None, "policy_rejected"

        # DNS root dots and fragments do not identify another source resource.
        parsed_canonical = urlsplit(str(parsed_url))
        rendered_host = f"[{host}]" if ":" in host else host
        default_port = 443 if parsed_canonical.scheme == "https" else 80
        netloc = (
            rendered_host
            if parsed_url.port == default_port
            else f"{rendered_host}:{parsed_url.port}"
        )
        canonical_text = urlunsplit(
            (
                parsed_canonical.scheme,
                netloc,
                _uppercase_percent_escapes(parsed_canonical.path),
                _uppercase_percent_escapes(parsed_canonical.query),
                "",
            )
        )
        canonical_url = _URL_ADAPTER.validate_python(canonical_text)
        return (
            SearchHit(
                title=title,
                url=canonical_url,
                snippet=snippet,
                rank=rank,
            ),
            None,
        )


def _attempt_outcome(result: _NormalizationResult) -> SearchAttemptOutcome:
    if result.hits:
        return SearchAttemptOutcome.SUCCESS
    if result.raw_rows == 0:
        return SearchAttemptOutcome.RAW_EMPTY
    if (
        result.policy_rejected_rows > 0
        and result.invalid_rows == 0
        and result.duplicate_rows == 0
    ):
        return SearchAttemptOutcome.ALL_POLICY_REJECTED
    return SearchAttemptOutcome.NORMALIZED_EMPTY


def _normalize_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:400] or None


def _uppercase_percent_escapes(value: str) -> str:
    """Normalize valid escapes only; invalid percent text keeps its identity."""

    return _PERCENT_ESCAPE.sub(lambda match: match.group(0).upper(), value)


def _duration_ms(started_ns: int) -> int:
    return min(
        max((monotonic_ns() - started_ns) // 1_000_000, 0),
        _MAX_ATTEMPT_DURATION_MS,
    )
