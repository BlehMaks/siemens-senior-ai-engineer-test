"""Bounded asynchronous adapter for the synchronous DDGS text API."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from search_agent.contracts import SearchHit, SearchQuery
from search_agent.security.site_policy import SafeSearch, SitePolicy

_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_ROWS_PER_REQUESTED_HIT = 4
_SAFE_SEARCH_ARGUMENT = {
    SafeSearch.STRICT: "on",
    SafeSearch.MODERATE: "moderate",
}


class SyncSearchBackend(Protocol):
    """The subset of ``DDGS`` used by the adapter."""

    def text(self, query: str, **kwargs: object) -> Sequence[object]: ...


class SearchFailure(RuntimeError):
    """A stable public failure that does not expose backend internals."""


@dataclass(frozen=True, slots=True)
class SearchAdapter:
    backend: SyncSearchBackend
    site_policy: SitePolicy
    region: str = "wt-wt"
    backend_name: str = "duckduckgo"

    async def search(self, query: SearchQuery) -> tuple[SearchHit, ...]:
        """Run blocking DDGS work off-loop and return bounded, policy-safe hits."""

        try:
            rows = await asyncio.to_thread(
                self.backend.text,
                query.text,
                region=self.region,
                safesearch=_SAFE_SEARCH_ARGUMENT[self.site_policy.safe_search],
                max_results=query.max_results,
                backend=self.backend_name,
            )
        except Exception:
            raise SearchFailure("search backend failed") from None

        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise SearchFailure("search backend returned an invalid result")
        try:
            return self._normalize(rows, query.max_results)
        except SearchFailure:
            raise
        except Exception:
            raise SearchFailure("search backend returned an invalid result") from None

    def _normalize(
        self, rows: Sequence[object], max_results: int
    ) -> tuple[SearchHit, ...]:
        hits: list[SearchHit] = []
        seen_urls: set[str] = set()

        # The backend was asked for max_results; a small allowance preserves useful
        # filtering without letting a hostile Sequence create unbounded work.
        iterator = iter(rows)
        for _ in range(max_results * _ROWS_PER_REQUESTED_HIT):
            try:
                row = next(iterator)
            except StopIteration:
                break
            normalized = self._normalize_row(row, rank=len(hits) + 1)
            if normalized is None:
                continue
            canonical_url = str(normalized.url)
            if canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)
            hits.append(normalized)
            if len(hits) == max_results:
                break

        return tuple(hits)

    def _normalize_row(self, row: object, *, rank: int) -> SearchHit | None:
        if not isinstance(row, Mapping):
            return None

        try:
            title = _normalize_text(row.get("title"))
            raw_href = row.get("href")
            snippet = _normalize_text(row.get("body"))
            if title is None or snippet is None or not isinstance(raw_href, str):
                return None
            href = raw_href.strip()
        except Exception:
            raise SearchFailure("search backend returned an invalid result") from None

        try:
            parsed_url = _URL_ADAPTER.validate_python(href)
        except ValidationError:
            return None
        if parsed_url.username is not None or parsed_url.password is not None:
            return None
        if parsed_url.port not in self.site_policy.allowed_ports:
            return None

        raw_host = parsed_url.host
        if raw_host is None:
            return None
        host = raw_host.strip("[]").rstrip(".")
        try:
            decision = self.site_policy.evaluate(host)
        except ValueError:
            return None
        if not decision.allowed:
            return None

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
        return SearchHit(
            title=title,
            url=canonical_url,
            snippet=snippet,
            rank=rank,
        )


def _normalize_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:400] or None


def _uppercase_percent_escapes(value: str) -> str:
    """Normalize valid escapes only; invalid percent text keeps its identity."""

    return _PERCENT_ESCAPE.sub(lambda match: match.group(0).upper(), value)
