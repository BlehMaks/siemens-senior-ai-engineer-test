from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import overload

import pytest

from search_agent.contracts import SearchQuery
from search_agent.security.site_policy import SafeSearch, SiteCategory, SitePolicy
from search_agent.tools.search import (
    SearchAdapter,
    SearchAttemptOutcome,
    SearchFailure,
    parse_search_backends,
)


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


@dataclass
class RoutingSearchBackend:
    responses: dict[str, Sequence[object] | Exception]
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def text(self, query: str, **kwargs: object) -> Sequence[object]:
        self.calls.append((query, kwargs))
        response = self.responses[str(kwargs["backend"])]
        if isinstance(response, Exception):
            raise response
        return response


class ExplodingMapping(dict[str, object]):
    def get(self, key: str, default: object = None) -> object:
        raise RuntimeError("backend-secret-must-not-escape")


class CountingSequence(Sequence[object]):
    def __init__(self, rows: Sequence[object]) -> None:
        self.rows = rows
        self.reads = 0

    @overload
    def __getitem__(self, index: int) -> object: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[object]: ...

    def __getitem__(self, index: int | slice) -> object | Sequence[object]:
        self.reads += 1
        return self.rows[index]

    def __len__(self) -> int:
        return len(self.rows)


class ExplodingSequence(Sequence[object]):
    @overload
    def __getitem__(self, index: int) -> object: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[object]: ...

    def __getitem__(self, index: int | slice) -> object | Sequence[object]:
        raise RuntimeError("sequence-secret-must-not-escape")

    def __len__(self) -> int:
        raise RuntimeError("sequence-secret-must-not-escape")

    def __iter__(self) -> Iterator[object]:
        return super().__iter__()


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
async def test_search_collapses_root_dot_idna_canonical_duplicates() -> None:
    backend = FakeSearchBackend(
        rows=(
            {
                "title": "First",
                "href": "HTTPS://BÜCHER.example.:443/report?year=2026#summary",
                "body": "first body",
            },
            {
                "title": "Duplicate",
                "href": "https://xn--bcher-kva.example/report?year=2026#details",
                "body": "duplicate body",
            },
        )
    )

    hits = await SearchAdapter(backend, SitePolicy()).search(
        SearchQuery(text="siemens annual report", max_results=5)
    )

    assert [(hit.title, str(hit.url), hit.rank) for hit in hits] == [
        ("First", "https://xn--bcher-kva.example/report?year=2026", 1)
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
async def test_search_scans_only_bounded_rows_and_stops_after_enough_hits() -> None:
    valid_first = CountingSequence(
        tuple(
            {
                "title": f"Result {index}",
                "href": f"https://example.com/{index}",
                "body": "body",
            }
            for index in range(1_000)
        )
    )
    hits = await SearchAdapter(
        FakeSearchBackend(rows=valid_first), SitePolicy()
    ).search(SearchQuery(text="siemens annual report", max_results=1))

    invalid = CountingSequence(("invalid row",) * 1_000)
    empty = await SearchAdapter(FakeSearchBackend(rows=invalid), SitePolicy()).search(
        SearchQuery(text="siemens annual report", max_results=2)
    )

    assert len(hits) == 1
    assert valid_first.reads == 1
    assert empty == ()
    assert invalid.reads == 8


@pytest.mark.asyncio
async def test_hostile_sequence_iteration_is_sanitized() -> None:
    with pytest.raises(SearchFailure, match="invalid result") as caught:
        await SearchAdapter(
            FakeSearchBackend(rows=ExplodingSequence()), SitePolicy()
        ).search(SearchQuery(text="siemens annual report", max_results=2))

    assert caught.value.__cause__ is None
    assert "sequence-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_percent_escape_case_is_canonical_in_path_and_query_only() -> None:
    backend = FakeSearchBackend(
        rows=(
            {
                "title": "First",
                "href": "https://example.com/report%2f2026?q=%3a&bad=%zz",
                "body": "first body",
            },
            {
                "title": "Duplicate",
                "href": "https://example.com/report%2F2026?q=%3A&bad=%zz",
                "body": "duplicate body",
            },
            {
                "title": "Invalid escape remains distinct",
                "href": "https://example.com/report%2F2026?q=%3A&bad=%ZZ",
                "body": "distinct body",
            },
        )
    )

    hits = await SearchAdapter(backend, SitePolicy()).search(
        SearchQuery(text="siemens annual report", max_results=5)
    )

    assert [str(hit.url) for hit in hits] == [
        "https://example.com/report%2F2026?q=%3A&bad=%zz",
        "https://example.com/report%2F2026?q=%3A&bad=%ZZ",
    ]
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
async def test_mapping_exception_becomes_sanitized_search_failure() -> None:
    backend = FakeSearchBackend(rows=(ExplodingMapping(),))

    with pytest.raises(SearchFailure, match="invalid result") as caught:
        await SearchAdapter(backend, SitePolicy()).search(
            SearchQuery(text="siemens annual report", max_results=2)
        )

    assert caught.value.__cause__ is None
    assert "backend-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_malformed_backend_envelope_becomes_search_failure() -> None:
    backend = FakeSearchBackend()
    backend.rows = "not a result sequence"

    with pytest.raises(SearchFailure, match="invalid result"):
        await SearchAdapter(backend, SitePolicy()).search(
            SearchQuery(text="siemens annual report", max_results=2)
        )


@pytest.mark.asyncio
async def test_search_uses_auto_by_default() -> None:
    backend = FakeSearchBackend()

    await SearchAdapter(backend, SitePolicy()).search(
        SearchQuery(text="siemens annual report", max_results=1)
    )

    assert backend.calls[0][1]["backend"] == "auto"
    assert backend.calls[0][1]["region"] == "us-en"


@pytest.mark.asyncio
async def test_search_falls_back_after_exception_without_amplifying_result_limit() -> (
    None
):
    backend = RoutingSearchBackend(
        responses={
            "brave": RuntimeError("private backend failure"),
            "auto": tuple(
                {
                    "title": f"Result {index}",
                    "href": f"https://example.com/{index}",
                    "body": "body",
                }
                for index in range(3)
            ),
        }
    )
    adapter = SearchAdapter(
        backend,
        SitePolicy(),
        backend_names=("brave", "auto"),
    )

    result = await adapter.search_with_metadata(
        SearchQuery(text="siemens annual report", max_results=2)
    )

    assert len(result.hits) == 2
    assert [call[1]["max_results"] for call in backend.calls] == [2, 2]
    assert [attempt.outcome for attempt in result.attempts] == [
        SearchAttemptOutcome.EXCEPTION,
        SearchAttemptOutcome.SUCCESS,
    ]
    assert result.attempts[0].raw_rows == 0
    assert result.attempts[1].accepted_hits == 2
    assert result.attempts[1].rejection_count == 0
    assert [attempt.reason_code for attempt in result.attempts] == [
        "exception",
        "success",
    ]
    assert all(0 <= attempt.duration_ms <= 600_000 for attempt in result.attempts)


@pytest.mark.asyncio
async def test_search_fallback_preserves_policy_and_reports_empty_reasons() -> None:
    backend = RoutingSearchBackend(
        responses={
            "auto": (
                {
                    "title": "Blocked",
                    "href": "https://blocked.example/report",
                    "body": "body",
                },
            ),
            "duckduckgo": (),
        }
    )
    adapter = SearchAdapter(
        backend,
        SitePolicy(denied_domains=frozenset({"blocked.example"})),
        backend_names=("auto", "duckduckgo"),
    )

    result = await adapter.search_with_metadata(
        SearchQuery(text="siemens annual report", max_results=2)
    )

    assert result.hits == ()
    assert [attempt.outcome for attempt in result.attempts] == [
        SearchAttemptOutcome.ALL_POLICY_REJECTED,
        SearchAttemptOutcome.RAW_EMPTY,
    ]
    assert result.attempts[0].rejection_count == 1
    assert result.attempts[1].rejection_count == 0


@pytest.mark.asyncio
async def test_search_reports_normalized_empty_before_successful_fallback() -> None:
    backend = RoutingSearchBackend(
        responses={
            "auto": ("invalid row",),
            "duckduckgo": (
                {
                    "title": "Allowed",
                    "href": "https://example.com/report",
                    "body": "body",
                },
            ),
        }
    )

    result = await SearchAdapter(
        backend,
        SitePolicy(),
        backend_names=("auto", "duckduckgo"),
    ).search_with_metadata(SearchQuery(text="siemens annual report", max_results=1))

    assert [attempt.outcome for attempt in result.attempts] == [
        SearchAttemptOutcome.NORMALIZED_EMPTY,
        SearchAttemptOutcome.SUCCESS,
    ]


@pytest.mark.asyncio
async def test_search_failure_carries_only_safe_attempt_metadata() -> None:
    backend = RoutingSearchBackend(
        responses={
            "auto": RuntimeError("credential=secret"),
            "duckduckgo": "invalid envelope",
        }
    )

    with pytest.raises(SearchFailure, match="invalid result") as caught:
        await SearchAdapter(
            backend,
            SitePolicy(),
            backend_names=("auto", "duckduckgo"),
        ).search(SearchQuery(text="siemens annual report", max_results=1))

    assert [attempt.outcome for attempt in caught.value.attempts] == [
        SearchAttemptOutcome.EXCEPTION,
        SearchAttemptOutcome.INVALID,
    ]
    assert "secret" not in str(caught.value)
    assert "secret" not in repr(caught.value.attempts)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("auto", ("auto",)),
        ("brave,auto", ("brave", "auto")),
        ("yahoo,auto", ("yahoo", "auto")),
        ("auto,duckduckgo", ("auto", "duckduckgo")),
        ("duckduckgo,auto", ("duckduckgo", "auto")),
    ],
)
def test_search_backend_parser_preserves_valid_order(
    value: str, expected: tuple[str, ...]
) -> None:
    assert parse_search_backends(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "auto,",
        "auto, auto",
        "auto,auto",
        "bing",
        "åuto",
        "auto,duckduckgo,auto",
        "a" * 65,
    ],
)
def test_search_backend_parser_rejects_invalid_or_unbounded_values(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        parse_search_backends(value)


def test_search_adapter_rejects_unbounded_or_unsupported_backend_order() -> None:
    backend = FakeSearchBackend()

    with pytest.raises(ValueError):
        SearchAdapter(backend, SitePolicy(), backend_names=("auto", "auto"))
    with pytest.raises(ValueError):
        SearchAdapter(backend, SitePolicy(), backend_names=("bing",))
