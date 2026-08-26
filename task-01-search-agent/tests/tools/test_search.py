from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from search_agent.contracts import SearchQuery
from search_agent.security.site_policy import SafeSearch, SiteCategory, SitePolicy
from search_agent.tools.search import SearchAdapter, SearchFailure


@dataclass
class FakeSearchBackend:
    rows: Sequence[object] = ()
    error: Exception | None = None
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    thread_ids: list[int] = field(default_factory=list)

    def text(self, query: str, **kwargs: object) -> Sequence[object]:
        self.calls.append((query, kwargs))
        self.thread_ids.append(threading.get_ident())
        if self.error is not None:
            raise self.error
        return self.rows


@pytest.mark.asyncio
async def test_search_passes_exact_bounded_ddgs_arguments_off_loop() -> None:
    backend = FakeSearchBackend(
        rows=(
            {
                "title": " Siemens   report ",
                "href": "https://Example.com/report",
                "body": " Annual\n sustainability report ",
            },
        )
    )
    adapter = SearchAdapter(
        backend,
        SitePolicy(),
        region="de-de",
        backend_name="duckduckgo",
    )
    main_thread_id = threading.get_ident()

    hits = await adapter.search(
        SearchQuery(text="siemens sustainability", max_results=3)
    )

    assert backend.calls == [
        (
            "siemens sustainability",
            {
                "region": "de-de",
                "safesearch": "on",
                "max_results": 3,
                "backend": "duckduckgo",
            },
        )
    ]
    assert backend.thread_ids != [main_thread_id]
    assert [(hit.title, str(hit.url), hit.snippet, hit.rank) for hit in hits] == [
        (
            "Siemens report",
            "https://example.com/report",
            "Annual sustainability report",
            1,
        )
    ]


@pytest.mark.asyncio
async def test_moderate_safe_search_maps_to_ddgs_argument() -> None:
    backend = FakeSearchBackend()
    adapter = SearchAdapter(
        backend,
        SitePolicy(safe_search=SafeSearch.MODERATE),
    )

    await adapter.search(SearchQuery(text="siemens annual report", max_results=1))

    assert backend.calls[0][1]["safesearch"] == "moderate"


@pytest.mark.asyncio
async def test_search_filters_before_ranking_and_preserves_first_canonical_url() -> (
    None
):
    backend = FakeSearchBackend(
        rows=(
            {"title": "Blocked", "href": "https://bad.example/a", "body": "x"},
            {"title": "Missing body", "href": "https://example.com/missing"},
            {
                "title": "First",
                "href": "HTTPS://Example.COM:443/report#summary",
                "body": "first body",
            },
            {
                "title": "Duplicate",
                "href": "https://example.com/report#details",
                "body": "duplicate body",
            },
            {
                "title": "Second",
                "href": "https://second.example/report",
                "body": "second body",
            },
        )
    )
    policy = SitePolicy(denied_domains=frozenset({"bad.example"}))

    hits = await SearchAdapter(backend, policy).search(
        SearchQuery(text="siemens annual report", max_results=5)
    )

    assert [(hit.title, str(hit.url), hit.rank) for hit in hits] == [
        ("First", "https://example.com/report", 1),
        ("Second", "https://second.example/report", 2),
    ]


@pytest.mark.asyncio
async def test_search_skips_malformed_and_policy_invalid_rows() -> None:
    backend = FakeSearchBackend(
        rows=(
            "not a mapping",
            {"title": 1, "href": "https://example.com", "body": "body"},
            {"title": "FTP", "href": "ftp://example.com/a", "body": "body"},
            {
                "title": "Invalid policy host",
                "href": "https://foo_bar.example/a",
                "body": "body",
            },
            {
                "title": "Credentials",
                "href": "https://user:secret@example.com/a",
                "body": "body",
            },
            {
                "title": "Port",
                "href": "https://example.com:8443/a",
                "body": "body",
            },
            {
                "title": "Category",
                "href": "https://adult.example/a",
                "body": "body",
            },
            {
                "title": "Allowed",
                "href": "http://example.com/a",
                "body": "body",
            },
        )
    )
    policy = SitePolicy(
        domain_categories={"adult.example": SiteCategory.ADULT},
    )

    hits = await SearchAdapter(backend, policy).search(
        SearchQuery(text="siemens annual report", max_results=5)
    )

    assert [hit.title for hit in hits] == ["Allowed"]


@pytest.mark.asyncio
async def test_search_never_exceeds_query_max_results() -> None:
    backend = FakeSearchBackend(
        rows=tuple(
            {
                "title": f"Result {index}",
                "href": f"https://example.com/{index}",
                "body": "body",
            }
            for index in range(5)
        )
    )

    hits = await SearchAdapter(backend, SitePolicy()).search(
        SearchQuery(text="siemens annual report", max_results=2)
    )

    assert len(hits) == 2
    assert [hit.rank for hit in hits] == [1, 2]


@pytest.mark.asyncio
async def test_backend_exception_becomes_sanitized_search_failure() -> None:
    backend = FakeSearchBackend(error=RuntimeError("credential=top-secret"))

    with pytest.raises(SearchFailure, match=r"^search backend failed$") as caught:
        await SearchAdapter(backend, SitePolicy()).search(
            SearchQuery(text="siemens annual report", max_results=2)
        )

    assert caught.value.__cause__ is None
    assert "top-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_malformed_backend_envelope_becomes_search_failure() -> None:
    backend = FakeSearchBackend()
    backend.rows = "not a result sequence"

    with pytest.raises(SearchFailure, match="invalid result"):
        await SearchAdapter(backend, SitePolicy()).search(
            SearchQuery(text="siemens annual report", max_results=2)
        )
