from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from search_agent import (
    FakeStructuredChatProvider,
    OllamaStructuredChatProvider,
    ProviderMessage,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransportError,
)


class PlanEnvelope(BaseModel):
    plan: str
    steps: int


def _messages() -> tuple[ProviderMessage, ...]:
    return (
        ProviderMessage(role="system", content="Return structured JSON only."),
        ProviderMessage(role="user", content="Plan a bounded Siemens report search."),
    )


@pytest.mark.asyncio
async def test_fake_provider_returns_validated_scripted_response() -> None:
    provider = FakeStructuredChatProvider(
        responses=[{"plan": "search report then compare years", "steps": 2}]
    )

    result = await provider.generate_structured(
        messages=_messages(),
        response_model=PlanEnvelope,
        temperature=0.0,
    )

    assert result.response == PlanEnvelope(
        plan="search report then compare years", steps=2
    )
    assert provider.calls[0].schema_name == "PlanEnvelope"
    assert result.metadata.provider_name == "fake"


@pytest.mark.asyncio
async def test_fake_provider_rejects_schema_mismatch() -> None:
    provider = FakeStructuredChatProvider(responses=[{"plan": "missing steps"}])

    with pytest.raises(ProviderResponseError, match="did not match schema"):
        await provider.generate_structured(
            messages=_messages(),
            response_model=PlanEnvelope,
        )


@pytest.mark.asyncio
async def test_ollama_provider_posts_schema_and_ignores_thinking() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "model": "llama3.1",
                "done_reason": "stop",
                "prompt_eval_count": 42,
                "eval_count": 8,
                "total_duration": 1234,
                "load_duration": 55,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"plan": "search report then compare years", "steps": 2}
                    ),
                    "thinking": "hidden chain of thought",
                },
            },
        )

    provider = OllamaStructuredChatProvider(
        model_name="llama3.1",
        transport=httpx.MockTransport(handler),
    )

    result = await provider.generate_structured(
        messages=_messages(),
        response_model=PlanEnvelope,
        temperature=0.2,
    )

    payload = captured["payload"]
    assert captured["path"] == "/api/chat"
    assert isinstance(payload, dict)
    assert payload["model"] == "llama3.1"
    assert payload["stream"] is False
    assert payload["think"] is False
    assert payload["format"] == PlanEnvelope.model_json_schema()
    assert result.response == PlanEnvelope(
        plan="search report then compare years", steps=2
    )
    assert result.metadata.model_name == "llama3.1"
    assert result.metadata.done_reason == "stop"
    assert result.metadata.prompt_eval_count == 42


@pytest.mark.asyncio
async def test_ollama_provider_retries_timeout_once() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(
            200,
            json={
                "model": "llama3.1",
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"plan": "retry worked", "steps": 1}),
                },
            },
        )

    provider = OllamaStructuredChatProvider(
        model_name="llama3.1",
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )

    result = await provider.generate_structured(
        messages=_messages(),
        response_model=PlanEnvelope,
    )

    assert attempts == 2
    assert result.response == PlanEnvelope(plan="retry worked", steps=1)
    assert result.metadata.attempt_count == 2


@pytest.mark.asyncio
async def test_ollama_provider_maps_terminal_timeout_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = OllamaStructuredChatProvider(
        model_name="llama3.1",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderTimeoutError, match="timed out"):
        await provider.generate_structured(
            messages=_messages(),
            response_model=PlanEnvelope,
        )


@pytest.mark.asyncio
async def test_ollama_provider_maps_http_and_schema_failures() -> None:
    async def http_error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model unavailable")

    provider = OllamaStructuredChatProvider(
        model_name="llama3.1",
        transport=httpx.MockTransport(http_error_handler),
    )

    with pytest.raises(ProviderResponseError, match="HTTP 503"):
        await provider.generate_structured(
            messages=_messages(),
            response_model=PlanEnvelope,
        )

    async def bad_json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "llama3.1",
                "message": {"role": "assistant", "content": "not json"},
            },
        )

    provider = OllamaStructuredChatProvider(
        model_name="llama3.1",
        transport=httpx.MockTransport(bad_json_handler),
    )

    with pytest.raises(ProviderResponseError, match="invalid structured content"):
        await provider.generate_structured(
            messages=_messages(),
            response_model=PlanEnvelope,
        )


@pytest.mark.asyncio
async def test_ollama_provider_maps_connection_failure_to_typed_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = OllamaStructuredChatProvider(
        model_name="llama3.1",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderTransportError, match="request failed"):
        await provider.generate_structured(
            messages=_messages(), response_model=PlanEnvelope
        )


@pytest.mark.asyncio
async def test_ollama_provider_rejects_non_object_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    provider = OllamaStructuredChatProvider(
        model_name="llama3.1", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(ProviderResponseError, match="not a JSON object"):
        await provider.generate_structured(
            messages=_messages(), response_model=PlanEnvelope
        )


@pytest.mark.asyncio
async def test_ollama_provider_does_not_echo_error_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="sensitive prompt and model reasoning")

    provider = OllamaStructuredChatProvider(
        model_name="llama3.1", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(ProviderResponseError, match="HTTP 503") as error:
        await provider.generate_structured(
            messages=_messages(), response_model=PlanEnvelope
        )

    assert "sensitive prompt" not in str(error.value)


@pytest.mark.parametrize("kwargs", [{"timeout_seconds": 0.0}, {"max_retries": -1}])
def test_ollama_provider_rejects_invalid_retry_configuration(
    kwargs: dict[str, float | int],
) -> None:
    with pytest.raises(ValueError):
        OllamaStructuredChatProvider(model_name="llama3.1", **kwargs)  # type: ignore[arg-type]
