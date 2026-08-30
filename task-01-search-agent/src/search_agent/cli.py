"""Command-line entry point for demo or real Ollama-backed research."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from .contracts import Citation, ScopedAnswer, SearchHit
from .evidence import build_evidence
from .model_auth import GoogleIdTokenAuth
from .planning import QueryPlanner
from .providers import FakeStructuredChatProvider
from .runner import ResearchRunner, RunBudget
from .runtime import (
    OllamaResearchExecutor,
    OllamaRuntimeSettings,
    search_backends_from_environment,
)
from .state import RunStatus
from .tools import ExtractedDocument, FetchedDocument
from .tools.search import parse_search_backends

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
        description="Run the bounded research agent in demo or Ollama mode.",
    )
    parser.add_argument("request", help="research request (3-400 characters)")
    parser.add_argument("--tenant-id", default="tenant-demo")
    parser.add_argument("--session-id", default="session-demo")
    parser.add_argument("--run-id", default="run-demo")
    parser.add_argument(
        "--mode",
        choices=("demo", "ollama"),
        default=os.environ.get("AGENT_INFERENCE_MODE", "demo"),
        help="demo is deterministic; ollama uses live model and web adapters",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("AGENT_MODEL_NAME"),
        help="Ollama model name (or AGENT_MODEL_NAME)",
    )
    parser.add_argument(
        "--ollama-base-url",
        default=os.environ.get("AGENT_MODEL_BASE_URL", "http://127.0.0.1:11434"),
        help="clean Ollama origin (or AGENT_MODEL_BASE_URL)",
    )
    parser.add_argument(
        "--model-transport-profile",
        choices=("local", "cloud"),
        default=os.environ.get("AGENT_MODEL_TRANSPORT_PROFILE", "local"),
        help="model transport security profile (or AGENT_MODEL_TRANSPORT_PROFILE)",
    )
    parser.add_argument(
        "--model-google-id-token-audience",
        default=os.environ.get("AGENT_MODEL_GOOGLE_ID_TOKEN_AUDIENCE"),
        help="cloud model ID-token audience (or AGENT_MODEL_GOOGLE_ID_TOKEN_AUDIENCE)",
    )
    parser.add_argument(
        "--search-backends",
        "--search-backend",
        type=_parse_search_backends,
        default=search_backends_from_environment(os.environ),
        help=(
            "ordered comma-separated search backends "
            "(AGENT_SEARCH_BACKENDS; legacy AGENT_SEARCH_BACKEND supported)"
        ),
    )
    parser.add_argument(
        "--action-log-level",
        choices=("ERROR", "WARNING", "INFO", "DEBUG"),
        default=os.environ.get("AGENT_ACTION_LOG_LEVEL", "INFO"),
        help="structured action-log verbosity (or AGENT_ACTION_LOG_LEVEL)",
    )
    return parser


def _parse_search_backends(value: str) -> tuple[str, ...]:
    try:
        return parse_search_backends(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


async def async_main(
    argv: Sequence[str] | None = None,
    *,
    runner: ResearchRunner | None = None,
) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.action_log_level),
        format="%(message)s",
    )
    if not 3 <= len(args.request.strip()) <= 400:
        print("request rejected by input policy", file=sys.stderr)
        return 2
    if runner is not None:
        selected_runner: ResearchRunner | OllamaResearchExecutor = runner
    elif args.mode == "demo":
        selected_runner = _demo_runner(args.request.strip())
    else:
        try:
            settings = OllamaRuntimeSettings(
                model_name=args.model or "",
                base_url=args.ollama_base_url,
                transport_profile=args.model_transport_profile,
                google_id_token_audience=args.model_google_id_token_audience,
                search_backends=args.search_backends,
            )
            model_auth = (
                GoogleIdTokenAuth(settings.google_id_token_audience)
                if settings.transport_profile == "cloud"
                and settings.google_id_token_audience is not None
                else None
            )
            selected_runner = OllamaResearchExecutor(settings, model_auth=model_auth)
        except (TypeError, ValueError):
            print("invalid Ollama runtime configuration", file=sys.stderr)
            return 2
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
