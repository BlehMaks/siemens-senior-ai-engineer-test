from __future__ import annotations

import json
import math
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ValidationError

from .base import (
    ProviderMessage,
    ProviderMetadata,
    ProviderResponseError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderTransportError,
    ThinkMode,
)


@dataclass(slots=True)
class OllamaStructuredChatProvider:
    model_name: str
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: float = 20.0
    max_retries: int = 1
    think: ThinkMode = False
    keep_alive: str | None = None
    transport: httpx.AsyncBaseTransport | None = None
    auth: httpx.Auth | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or not 0 <= self.max_retries <= 5
        ):
            raise ValueError("max_retries must be an integer between 0 and 5")

    async def generate_structured(
        self,
        *,
        messages: tuple[ProviderMessage, ...],
        response_model: type[BaseModel],
        temperature: float = 0.0,
    ) -> ProviderResult:
        attempts = 0
        while True:
            attempts += 1
            try:
                payload = self._build_payload(
                    messages=messages,
                    response_model=response_model,
                    temperature=temperature,
                )
                response = await self._post_chat(payload)
                return self._parse_response(
                    response=response,
                    response_model=response_model,
                    attempt_count=attempts,
                )
            except httpx.TimeoutException as exc:
                if attempts > self.max_retries:
                    raise ProviderTimeoutError("ollama request timed out") from exc
            except httpx.RequestError as exc:
                if attempts > self.max_retries:
                    raise ProviderTransportError("ollama request failed") from exc
            except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
                raise ProviderResponseError(
                    "ollama returned invalid structured content",
                    metadata=ProviderMetadata(
                        provider_name="ollama",
                        model_name=self.model_name,
                        attempt_count=attempts,
                    ),
                ) from exc

    def _build_payload(
        self,
        *,
        messages: tuple[ProviderMessage, ...],
        response_model: type[BaseModel],
        temperature: float,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model_name,
            "messages": [message.model_dump(mode="json") for message in messages],
            "format": response_model.model_json_schema(),
            "stream": False,
            "options": {"temperature": temperature},
        }
        if self.think is not None:
            payload["think"] = self.think
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        return payload

    async def _post_chat(self, payload: dict[str, object]) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
            auth=self.auth,
        ) as client:
            response = await client.post("/api/chat", json=payload)

        if response.status_code != httpx.codes.OK:
            raise ProviderResponseError(f"ollama returned HTTP {response.status_code}")
        return response

    def _parse_response(
        self,
        *,
        response: httpx.Response,
        response_model: type[BaseModel],
        attempt_count: int,
    ) -> ProviderResult:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderResponseError("ollama response was not a JSON object")
        message = payload.get("message")
        if not isinstance(message, dict):
            raise ProviderResponseError(
                "ollama response did not include a message object"
            )

        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderResponseError(
                "ollama response did not include structured content"
            )

        parsed = response_model.model_validate_json(content)
        return ProviderResult(
            response=parsed,
            metadata=ProviderMetadata(
                provider_name="ollama",
                model_name=str(payload.get("model", self.model_name)),
                attempt_count=attempt_count,
                done_reason=_optional_str(payload.get("done_reason")),
                prompt_eval_count=_optional_int(payload.get("prompt_eval_count")),
                eval_count=_optional_int(payload.get("eval_count")),
                total_duration_ns=_optional_int(payload.get("total_duration")),
                load_duration_ns=_optional_int(payload.get("load_duration")),
            ),
        )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
