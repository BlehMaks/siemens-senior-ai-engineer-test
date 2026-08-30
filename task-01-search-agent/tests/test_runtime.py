from __future__ import annotations

import json
from collections.abc import Sequence

import httpx
import pytest

from search_agent import OllamaResearchExecutor, OllamaRuntimeSettings, RunStatus


class _Resolver:
    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        assert (host, port) == ("example.com", 443)
        return ("93.184.216.34",)


class _SearchBackend:
    def __init__(self, rows: Sequence[object]) -> None:
        self.rows = rows
        self.calls = 0

    def text(self, query: str, **kwargs: object) -> Sequence[object]:
        self.calls += 1
        assert query
        assert kwargs["max_results"] == 1
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
async def test_real_runtime_searches_fetches_extracts_and_synthesizes() -> None:
    claim = "Siemens reduced operational emissions by 20 percent."
    source_url = "https://example.com/report"
    search = _SearchBackend(
        (
            {
                "title": "Sustainability report",
                "href": source_url,
                "body": claim,
            },
        )
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
                    "answer_focus": "Siemens operational emissions reduction",
                    "query_plan": {
                        "tool_budget": {
                            "max_search_queries": 1,
                            "max_fetches": 1,
                        },
                        "searches": [
                            {
                                "text": "Siemens operational emissions reduction",
                                "max_results": 1,
                            }
                        ],
                    },
                    "assistance": None,
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
        request="Research Siemens operational emissions reduction",
    )

    assert result.snapshot.status is RunStatus.COMPLETED
    assert result.snapshot.answer is not None
    assert result.snapshot.answer.answer_text == claim
    assert str(result.snapshot.answer.citations[0].source_url) == source_url
    assert search.calls == 1
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
