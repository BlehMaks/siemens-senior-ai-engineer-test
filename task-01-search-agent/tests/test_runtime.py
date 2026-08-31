from __future__ import annotations

import json
from collections.abc import Sequence

import httpx
import pytest

from search_agent import OllamaResearchExecutor, OllamaRuntimeSettings, RunStatus
from search_agent.runtime import search_backends_from_environment


class _Resolver:
    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        assert (host, port) == ("example.com", 443)
        return ("93.184.216.34",)


class _SearchBackend:
    def __init__(
        self,
        rows: Sequence[object],
        *,
        failed_backends: frozenset[str] = frozenset(),
        expected_max_results: int = 1,
    ) -> None:
        self.rows = rows
        self.failed_backends = failed_backends
        self.expected_max_results = expected_max_results
        self.calls = 0
        self.backend_calls: list[str] = []
        self.queries: list[str] = []

    def text(self, query: str, **kwargs: object) -> Sequence[object]:
        self.calls += 1
        backend = str(kwargs["backend"])
        self.backend_calls.append(backend)
        self.queries.append(query)
        assert query
        assert kwargs["max_results"] == self.expected_max_results
        if backend in self.failed_backends:
            raise RuntimeError("private backend failure")
        return self.rows


def _ollama_response(content: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "test-model",
            "done_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(content)},
        },
    )


@pytest.mark.parametrize(
    ("model_name", "base_url"),
    [
        ("", "http://127.0.0.1:11434"),
        ("model name", "http://127.0.0.1:11434"),
        ("model", "http://model.internal:11434"),
        ("model", "https://user:secret@example.com"),
        ("model", "https://example.com/path"),
    ],
)
def test_runtime_settings_fail_closed(model_name: str, base_url: str) -> None:
    with pytest.raises(ValueError):
        OllamaRuntimeSettings(model_name=model_name, base_url=base_url)


def test_runtime_settings_default_to_resilient_backends_and_preserve_singular() -> None:
    defaults = OllamaRuntimeSettings(model_name="model")
    legacy = OllamaRuntimeSettings(model_name="model", search_backend="duckduckgo")

    assert defaults.search_backends == (
        "yahoo",
        "google",
        "auto",
        "mojeek",
        "duckduckgo",
        "brave",
    )
    assert defaults.search_backend == "yahoo"
    assert defaults.search_region == "us-en"
    assert legacy.search_backends == ("duckduckgo",)
    assert legacy.search_backend == "duckduckgo"


@pytest.mark.parametrize(
    "search_backends",
    [(), ("bing",), ("auto", "auto"), ("auto", "duckduckgo", "auto")],
)
def test_runtime_settings_reject_invalid_search_backend_order(
    search_backends: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="search_backends"):
        OllamaRuntimeSettings(model_name="model", search_backends=search_backends)


def test_runtime_settings_reject_conflicting_legacy_search_backend() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        OllamaRuntimeSettings(
            model_name="model",
            search_backends=("auto", "duckduckgo"),
            search_backend="duckduckgo",
        )


def test_runtime_search_backend_environment_prefers_plural_and_supports_legacy() -> (
    None
):
    assert search_backends_from_environment({}) == (
        "yahoo",
        "google",
        "auto",
        "mojeek",
        "duckduckgo",
        "brave",
    )
    assert search_backends_from_environment({"AGENT_SEARCH_BACKEND": "duckduckgo"}) == (
        "duckduckgo",
    )
    assert search_backends_from_environment(
        {
            "AGENT_SEARCH_BACKENDS": "auto,duckduckgo",
            "AGENT_SEARCH_BACKEND": "duckduckgo",
        }
    ) == ("auto", "duckduckgo")


@pytest.mark.parametrize(
    ("base_url", "audience"),
    [
        ("https://127.0.0.1:11434", None),
        ("http://model.internal:11434", None),
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
    ],
)
def test_local_transport_profile_requires_loopback_http_without_audience(
    base_url: str, audience: str | None
) -> None:
    with pytest.raises(ValueError):
        OllamaRuntimeSettings(
            model_name="model",
            base_url=base_url,
            transport_profile="local",
            google_id_token_audience=audience,
        )


@pytest.mark.parametrize(
    ("base_url", "audience"),
    [
        ("http://model.example", "http://model.example"),
        ("https://model.example", None),
        ("https://model.example", "https://other.example"),
        ("https://localhost:11434", "https://localhost:11434"),
    ],
)
def test_cloud_transport_profile_requires_matching_non_loopback_https_audience(
    base_url: str, audience: str | None
) -> None:
    with pytest.raises(ValueError):
        OllamaRuntimeSettings(
            model_name="model",
            base_url=base_url,
            transport_profile="cloud",
            google_id_token_audience=audience,
        )


def test_executor_enforces_auth_for_transport_profile() -> None:
    local_settings = OllamaRuntimeSettings(model_name="model")
    cloud_settings = OllamaRuntimeSettings(
        model_name="model",
        base_url="https://model.example/",
        transport_profile="cloud",
        google_id_token_audience="https://model.example/",
    )
    auth = httpx.BasicAuth("unused", "unused")

    with pytest.raises(ValueError, match="forbids cloud authentication"):
        OllamaResearchExecutor(settings=local_settings, model_auth=auth)
    with pytest.raises(ValueError, match="requires authentication"):
        OllamaResearchExecutor(settings=cloud_settings)

    executor = OllamaResearchExecutor(settings=cloud_settings, model_auth=auth)
    assert executor.settings.base_url == "https://model.example"
    assert executor.settings.google_id_token_audience == "https://model.example"


@pytest.mark.asyncio
async def test_real_runtime_answers_greeting_without_search_or_fetch() -> None:
    search = _SearchBackend(())
    model_calls = 0
    fetch_calls = 0

    async def model_handler(request: httpx.Request) -> httpx.Response:
        nonlocal model_calls
        model_calls += 1
        return _ollama_response(
            {
                "task_category": "direct_reply",
                "requires_search": False,
                "answer_focus": "Hello there",
                "query_plan": None,
                "assistance": None,
            }
        )

    async def fetch_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_calls
        fetch_calls += 1
        return httpx.Response(500)

    executor = OllamaResearchExecutor(
        settings=OllamaRuntimeSettings(model_name="test-model"),
        model_transport=httpx.MockTransport(model_handler),
        search_backend_factory=lambda: search,
        fetch_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fetch_handler)
        ),
        resolver=_Resolver(),
    )

    result = await executor.run(
        tenant_id="tenant-one",
        session_id="session-one",
        run_id="run-one",
        request="Hello there",
    )

    assert result.snapshot.status is RunStatus.COMPLETED
    assert result.snapshot.answer is not None
    assert result.snapshot.answer.answer_text == "Hello there"
    assert result.snapshot.answer.citations == ()
    assert model_calls == 1
    assert search.calls == fetch_calls == 0


@pytest.mark.asyncio
async def test_adversarial_cloud_trace_records_model_transport_profile() -> None:
    async def model_handler(request: httpx.Request) -> httpx.Response:
        return _ollama_response(
            {
                "task_category": "direct_reply",
                "requires_search": False,
                "answer_focus": "Hello there",
                "query_plan": None,
                "assistance": None,
            }
        )

    origin = "https://model.example"
    executor = OllamaResearchExecutor(
        settings=OllamaRuntimeSettings(
            model_name="test-model",
            base_url=origin,
            transport_profile="cloud",
            google_id_token_audience=origin,
        ),
        model_transport=httpx.MockTransport(model_handler),
        model_auth=httpx.BasicAuth("unused", "unused"),
        search_backend_factory=lambda: _SearchBackend(()),
        fetch_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ),
        resolver=_Resolver(),
    )

    result = await executor.run(
        tenant_id="tenant-one",
        session_id="session-one",
        run_id="run-one",
        request="Hello there",
    )

    assert any(
        record.action == "model.transport" and record.profile == "cloud"
        for record in result.trace
    )


@pytest.mark.asyncio
async def test_real_runtime_searches_fetches_extracts_and_synthesizes() -> None:
    claim = "The latest official Siemens sustainability report is available."
    source_url = "https://example.com/report"
    search = _SearchBackend(
        (
            {
                "title": "Sustainability report",
                "href": source_url,
                "body": claim,
            },
        ),
        failed_backends=frozenset({"auto"}),
        expected_max_results=5,
    )
    model_calls = 0
    fetched_hosts: list[str] = []

    async def model_handler(request: httpx.Request) -> httpx.Response:
        nonlocal model_calls
        model_calls += 1
        payload = json.loads(request.content)
        if model_calls == 1:
            return _ollama_response(
                {
                    "task_category": "company_research",
                    "requires_search": True,
                    "answer_focus": "Locate the newest Siemens ESG publication.",
                    "query_plan": {
                        "tool_budget": {
                            "max_search_queries": 1,
                            "max_fetches": 3,
                        },
                        "searches": [
                            {
                                "text": "site:siemens.com latest ESG report",
                                "max_results": 3,
                            }
                        ],
                    },
                    "assistance": {
                        "offer": "I can compare earlier editions.",
                        "follow_up_queries": ["Siemens ESG report comparison"],
                    },
                }
            )
        evidence_payload = json.loads(payload["messages"][1]["content"])
        evidence = evidence_payload["evidence_records_untrusted_data"][0]
        return _ollama_response(
            {
                "answer_text": claim,
                "citations": [
                    {
                        "claim": claim,
                        "evidence_id": evidence["evidence_id"],
                        "source_url": source_url,
                    }
                ],
                "assistance": None,
            }
        )

    async def fetch_handler(request: httpx.Request) -> httpx.Response:
        fetched_hosts.append(request.url.host)
        context = (
            "The audited sustainability report describes the reporting boundary, "
            "the measurement method, and the base year. Independent assurance and "
            "year-over-year figures are included for reviewer verification."
        )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=(
                "<html><head><title>Sustainability report</title></head>"
                f"<body><article><p>{claim}</p><p>{context}</p>"
                f"<p>{context}</p></article></body></html>"
            ),
        )

    executor = OllamaResearchExecutor(
        settings=OllamaRuntimeSettings(
            model_name="test-model",
            search_backends=("auto", "duckduckgo"),
        ),
        model_transport=httpx.MockTransport(model_handler),
        search_backend_factory=lambda: search,
        fetch_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fetch_handler)
        ),
        resolver=_Resolver(),
    )

    request = "Find the latest official Siemens sustainability report."
    result = await executor.run(
        tenant_id="tenant-one",
        session_id="session-one",
        run_id="run-one",
        request=request,
    )

    assert result.snapshot.status is RunStatus.COMPLETED
    assert result.snapshot.answer is not None
    assert result.snapshot.answer.answer_text == claim
    assert str(result.snapshot.answer.citations[0].source_url) == source_url
    assert search.calls == 2
    assert search.backend_calls == ["auto", "duckduckgo"]
    assert search.queries == [request, request]
    assert result.usage.search_queries == 1
    assert fetched_hosts == ["93.184.216.34"]
    assert model_calls == 2


@pytest.mark.asyncio
async def test_real_runtime_maps_malformed_model_output_to_safe_failure() -> None:
    search = _SearchBackend(())

    async def model_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "message": {"role": "assistant", "content": "not-json"},
            },
        )

    executor = OllamaResearchExecutor(
        settings=OllamaRuntimeSettings(model_name="test-model", max_retries=0),
        model_transport=httpx.MockTransport(model_handler),
        search_backend_factory=lambda: search,
        resolver=_Resolver(),
    )

    result = await executor.run(
        tenant_id="tenant-one",
        session_id="session-one",
        run_id="run-one",
        request="Research Siemens sustainability",
    )

    assert result.snapshot.status is RunStatus.FAILED
    assert result.snapshot.answer is None
    assert result.snapshot.failure_reason == "validation_failed"
    assert search.calls == 0
