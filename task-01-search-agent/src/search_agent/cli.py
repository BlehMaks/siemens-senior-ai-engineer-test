"""Minimal offline demonstration entry point for the bounded runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from .contracts import Citation, ScopedAnswer, SearchHit
from .evidence import build_evidence
from .planning import QueryPlanner
from .providers import FakeStructuredChatProvider
from .runner import ResearchRunner, RunBudget
from .state import RunStatus
from .tools import ExtractedDocument, FetchedDocument

_URL = "https://www.siemens.com/sustainability-report"
_TITLE = "Siemens sustainability report"
_SOURCE_TEXT = "Siemens publishes a sustainability report."
_URL_ADAPTER = TypeAdapter(AnyHttpUrl)


class _DemoSearch:
    async def search(self, query: object) -> tuple[SearchHit, ...]:
        return (
            SearchHit(
                title=_TITLE,
                url=_URL_ADAPTER.validate_python(_URL),
                snippet=_SOURCE_TEXT,
                rank=1,
            ),
        )


class _DemoFetcher:
    async def fetch(self, raw_url: str) -> FetchedDocument:
        return FetchedDocument(
            canonical_url=_URL,
            content_type="text/html",
            body=_SOURCE_TEXT.encode(),
        )


class _DemoExtractor:
    async def extract(self, document: FetchedDocument) -> ExtractedDocument:
        return ExtractedDocument(
            canonical_url=document.canonical_url,
            title=_TITLE,
            text=document.body.decode(),
        )


def _demo_runner(request: str) -> ResearchRunner:
    hit = SearchHit(
        title=_TITLE,
        url=_URL_ADAPTER.validate_python(_URL),
        snippet=_SOURCE_TEXT,
        rank=1,
    )
    document = ExtractedDocument(
        canonical_url=_URL,
        title=_TITLE,
        text=_SOURCE_TEXT,
    )
    evidence_id = build_evidence(
        hit,
        document,
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    ).evidence_id
    provider = FakeStructuredChatProvider(
        responses=[
            {
                "task_category": "company_research",
                "requires_search": True,
                "answer_focus": request,
                "query_plan": {
                    "tool_budget": {"max_search_queries": 1, "max_fetches": 1},
                    "searches": [{"text": request, "max_results": 1}],
                },
            },
            ScopedAnswer(
                answer_text=_SOURCE_TEXT,
                citations=(
                    Citation(
                        claim=_SOURCE_TEXT,
                        evidence_id=evidence_id,
                        source_url=_URL_ADAPTER.validate_python(_URL),
                    ),
                ),
            ).model_dump(mode="python"),
        ]
    )
    return ResearchRunner(
        planner=QueryPlanner(provider),
        searcher=_DemoSearch(),
        fetcher=_DemoFetcher(),
        extractor=_DemoExtractor(),
        provider=provider,
        fetch_reservation_bytes=len(_SOURCE_TEXT.encode()),
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="siemens-search-agent",
        description="Run the deterministic offline research-agent demonstration.",
    )
    parser.add_argument("request", help="research request (3-400 characters)")
    parser.add_argument("--tenant-id", default="tenant-demo")
    parser.add_argument("--session-id", default="session-demo")
    parser.add_argument("--run-id", default="run-demo")
    return parser


async def async_main(
    argv: Sequence[str] | None = None,
    *,
    runner: ResearchRunner | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if not 3 <= len(args.request.strip()) <= 400:
        print("request rejected by input policy", file=sys.stderr)
        return 2
    selected_runner = runner or _demo_runner(args.request.strip())
    try:
        result = await selected_runner.run(
            tenant_id=args.tenant_id,
            session_id=args.session_id,
            run_id=args.run_id,
            request=args.request.strip(),
            budget=RunBudget(),
        )
    except (TypeError, ValueError, ValidationError):
        print("request rejected by input policy", file=sys.stderr)
        return 2

    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    if result.snapshot.status is RunStatus.COMPLETED:
        return 0
    if result.snapshot.status is RunStatus.CANCELLED:
        return 2
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
