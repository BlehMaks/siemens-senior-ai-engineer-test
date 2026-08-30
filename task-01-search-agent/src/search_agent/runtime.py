"""Production composition for an Ollama-backed internet research agent."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address
from math import isfinite
from typing import cast
from urllib.parse import urlsplit

import httpx
from ddgs import DDGS

from .contracts import OpaqueId, QueryText
from .memory import RepositoryReviewedMemoryReader
from .planning import QueryPlanner
from .providers import OllamaStructuredChatProvider
from .runner import ResearchRunner, RunBudget, RunResult
from .security import HostResolver, SitePolicy, UrlGuard
from .tools import (
    AsyncLocalExtractor,
    GuardedFetcher,
    SearchAdapter,
    SyncSearchBackend,
    create_fetch_client,
)

_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
SearchBackendFactory = Callable[[], SyncSearchBackend]
FetchClientFactory = Callable[[], httpx.AsyncClient]
MemoryReaderFactory = Callable[[], RepositoryReviewedMemoryReader]


def _ddgs_backend() -> SyncSearchBackend:
    return cast(SyncSearchBackend, DDGS())


@dataclass(frozen=True, slots=True)
class OllamaRuntimeSettings:
    """Validated settings shared by the CLI and Task 3 worker container."""

    model_name: str
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: float = 120.0
    max_retries: int = 1
    search_region: str = "wt-wt"
    search_backend: str = "duckduckgo"

    def __post_init__(self) -> None:
        if not _MODEL_NAME.fullmatch(self.model_name):
            raise ValueError("model_name has an invalid format")
        normalized_url = self.base_url.rstrip("/")
        parsed = urlsplit(normalized_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("base_url must be a clean HTTP(S) origin")
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
            raise ValueError("unencrypted model transport is allowed only on loopback")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not isfinite(float(self.timeout_seconds))
            or not 1.0 <= float(self.timeout_seconds) <= 600.0
        ):
            raise ValueError("timeout_seconds must be between 1 and 600")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or not 0 <= self.max_retries <= 5
        ):
            raise ValueError("max_retries must be between 0 and 5")
        for name, value in (
            ("search_region", self.search_region),
            ("search_backend", self.search_backend),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value.strip() != value
                or len(value) > 40
                or not value.isascii()
            ):
                raise ValueError(f"{name} must be a short clean ASCII value")
        object.__setattr__(self, "base_url", normalized_url)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


@dataclass(frozen=True, slots=True)
class OllamaResearchExecutor:
    """Create one bounded runner per request around an already-running Ollama."""

    settings: OllamaRuntimeSettings
    model_transport: httpx.AsyncBaseTransport | None = None
    model_auth: httpx.Auth | None = None
    search_backend_factory: SearchBackendFactory = _ddgs_backend
    fetch_client_factory: FetchClientFactory = create_fetch_client
    resolver: HostResolver | None = None
    memory_reader_factory: MemoryReaderFactory | None = None

    async def run(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        request: QueryText | str,
        budget: RunBudget | None = None,
    ) -> RunResult:
        memory_reader = (
            None if self.memory_reader_factory is None else self.memory_reader_factory()
        )
        try:
            async with self.fetch_client_factory() as fetch_client:
                runner = self._runner(fetch_client, memory_reader=memory_reader)
                return await runner.run(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    run_id=run_id,
                    request=str(request),
                    budget=budget,
                )
        finally:
            if memory_reader is not None:
                memory_reader.close()
                for repository in (
                    memory_reader.semantic_facts,
                    memory_reader.procedures,
                ):
                    close = getattr(repository, "close", None)
                    if callable(close):
                        close()

    def _runner(
        self,
        fetch_client: httpx.AsyncClient,
        *,
        memory_reader: RepositoryReviewedMemoryReader | None,
    ) -> ResearchRunner:
        policy = SitePolicy()
        guard = (
            UrlGuard(policy=policy)
            if self.resolver is None
            else UrlGuard(policy=policy, resolver=self.resolver)
        )
        provider = OllamaStructuredChatProvider(
            model_name=self.settings.model_name,
            base_url=self.settings.base_url,
            timeout_seconds=self.settings.timeout_seconds,
            max_retries=self.settings.max_retries,
            transport=self.model_transport,
            auth=self.model_auth,
        )
        return ResearchRunner(
            planner=QueryPlanner(provider),
            searcher=SearchAdapter(
                backend=self.search_backend_factory(),
                site_policy=policy,
                region=self.settings.search_region,
                backend_name=self.settings.search_backend,
            ),
            fetcher=GuardedFetcher(client=fetch_client, guard=guard),
            extractor=AsyncLocalExtractor(),
            provider=provider,
            memory_reader=memory_reader,
            memory_reads_enabled=memory_reader is not None,
        )


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False
