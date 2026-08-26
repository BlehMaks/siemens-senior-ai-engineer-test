"""Bounded asynchronous adapter for the synchronous DDGS text API."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from search_agent.contracts import SearchHit, SearchQuery
from search_agent.security.site_policy import SafeSearch, SitePolicy

_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
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
        return self._normalize(rows, query.max_results)

    def _normalize(
        self, rows: Sequence[object], max_results: int
    ) -> tuple[SearchHit, ...]:
        hits: list[SearchHit] = []
        seen_urls: set[str] = set()

        for row in rows:
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

        title = _normalize_text(row.get("title"))
        href = row.get("href")
        snippet = _normalize_text(row.get("body"))
        if title is None or snippet is None or not isinstance(href, str):
            return None

        try:
            parsed_url = _URL_ADAPTER.validate_python(href.strip())
        except ValidationError:
            return None
        if parsed_url.username is not None or parsed_url.password is not None:
            return None
        if parsed_url.port not in self.site_policy.allowed_ports:
            return None

        host = parsed_url.host
        if host is None:
            return None
        try:
            decision = self.site_policy.evaluate(host.strip("[]"))
        except ValueError:
            return None
        if not decision.allowed:
            return None

        # Fragments identify a location within the same resource, not another source.
        canonical_text = str(parsed_url).partition("#")[0]
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
