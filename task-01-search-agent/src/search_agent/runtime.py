"""Production composition for an Ollama-backed internet research agent."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from math import isfinite
from typing import Literal, cast
from urllib.parse import SplitResult, urlsplit

import httpx
from ddgs import DDGS

from .contracts import ConversationTurn, OpaqueId, QueryText
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
from .tools.search import parse_search_backends, validate_search_backends

_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
SearchBackendFactory = Callable[[], SyncSearchBackend]
FetchClientFactory = Callable[[], httpx.AsyncClient]
MemoryReaderFactory = Callable[[], RepositoryReviewedMemoryReader]


def _ddgs_backend() -> SyncSearchBackend:
    return cast(SyncSearchBackend, DDGS())


def search_backends_from_environment(
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    """Read the preferred plural env contract, then the legacy singular name."""

    configured = environment.get("AGENT_SEARCH_BACKENDS")
    if configured is None:
        configured = environment.get("AGENT_SEARCH_BACKEND", "auto")
    return parse_search_backends(configured)


@dataclass(frozen=True, slots=True)
class OllamaRuntimeSettings:
    """Validated settings shared by the CLI and Task 3 worker container."""

    model_name: str
    base_url: str = "http://127.0.0.1:11434"
    transport_profile: Literal["local", "cloud"] = "local"
    google_id_token_audience: str | None = None
    timeout_seconds: float = 120.0
    max_retries: int = 1
    search_region: str = "wt-wt"
    search_backends: tuple[str, ...] = ("auto",)
    search_backend: str | None = None

    def __post_init__(self) -> None:
        if not _MODEL_NAME.fullmatch(self.model_name):
            raise ValueError("model_name has an invalid format")
        normalized_url, parsed = _normalized_origin(self.base_url, name="base_url")
        audience = None
        if self.google_id_token_audience is not None:
            audience, _ = _normalized_origin(
                self.google_id_token_audience,
                name="google_id_token_audience",
            )
        hostname = parsed.hostname
        assert hostname is not None
        if self.transport_profile == "local":
            if parsed.scheme != "http" or not _is_loopback(hostname):
                raise ValueError("local model transport requires loopback HTTP")
            if audience is not None:
                raise ValueError("local model transport forbids a cloud audience")
        elif self.transport_profile == "cloud":
            if parsed.scheme != "https" or _is_loopback(hostname):
                raise ValueError("cloud model transport requires non-loopback HTTPS")
            if audience != normalized_url:
                raise ValueError("cloud model audience must exactly match base_url")
        else:
            raise ValueError("transport_profile must be local or cloud")
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
        for name, value in (("search_region", self.search_region),):
            if (
                not isinstance(value, str)
                or not value
                or value.strip() != value
                or len(value) > 40
                or not value.isascii()
            ):
                raise ValueError(f"{name} must be a short clean ASCII value")
        backends = self.search_backends
        if self.search_backend is not None:
            if backends != ("auto",) and backends != (self.search_backend,):
                raise ValueError("search_backend conflicts with search_backends")
            backends = (self.search_backend,)
        if not isinstance(backends, tuple):
            raise ValueError("search_backends must be an ordered tuple")
        try:
            backends = validate_search_backends(backends)
        except ValueError:
            raise ValueError(
                "search_backends must be an ordered supported backend tuple"
            ) from None
        object.__setattr__(self, "base_url", normalized_url)
        object.__setattr__(self, "google_id_token_audience", audience)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        object.__setattr__(self, "search_backends", backends)
        object.__setattr__(self, "search_backend", backends[0])


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

    def __post_init__(self) -> None:
        if self.settings.transport_profile == "local" and self.model_auth is not None:
            raise ValueError("local model transport forbids cloud authentication")
        if self.settings.transport_profile == "cloud" and self.model_auth is None:
            raise ValueError("cloud model transport requires authentication")

    async def run(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        request: QueryText | str,
        budget: RunBudget | None = None,
    ) -> RunResult:
        return await self.run_with_context(
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
            request=request,
            conversation_context=(),
            budget=budget,
        )

    async def run_with_context(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        request: QueryText | str,
        conversation_context: tuple[ConversationTurn, ...],
        budget: RunBudget | None = None,
    ) -> RunResult:
        memory_reader = (
            None if self.memory_reader_factory is None else self.memory_reader_factory()
        )
        try:
            async with self.fetch_client_factory() as fetch_client:
                runner = self._runner(fetch_client, memory_reader=memory_reader)
                return await runner.run_with_context(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    run_id=run_id,
                    request=str(request),
                    conversation_context=conversation_context,
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
                backend_names=self.settings.search_backends,
            ),
            fetcher=GuardedFetcher(client=fetch_client, guard=guard),
            extractor=AsyncLocalExtractor(),
            provider=provider,
            memory_reader=memory_reader,
            memory_reads_enabled=memory_reader is not None,
            model_transport_profile=self.settings.transport_profile,
        )


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _normalized_origin(value: str, *, name: str) -> tuple[str, SplitResult]:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a clean HTTP(S) origin")
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{name} must be a clean HTTP(S) origin")
    return normalized, parsed
