from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from .base import (
    BaseScriptedProvider,
    ProviderInvocation,
    ProviderMessage,
    ProviderMetadata,
    ProviderResponseError,
    ProviderResult,
)


@dataclass
class FakeStructuredChatProvider(BaseScriptedProvider):
    responses: list[object] = field(default_factory=list)
    model_name: str = "fake-structured-chat"

    async def generate_structured(
        self,
        *,
        messages: tuple[ProviderMessage, ...],
        response_model: type[BaseModel],
        temperature: float = 0.0,
    ) -> ProviderResult:
        self.calls.append(
            ProviderInvocation(
                messages=messages,
                schema_name=response_model.__name__,
                temperature=temperature,
            )
        )
        if not self.responses:
            raise ProviderResponseError("fake provider exhausted scripted responses")

        raw_response = self.responses.pop(0)
        try:
            parsed = response_model.model_validate(raw_response)
        except ValidationError as exc:
            raise ProviderResponseError(
                "fake provider response did not match schema"
            ) from exc

        return ProviderResult(
            response=parsed,
            metadata=ProviderMetadata(
                provider_name="fake",
                model_name=self.model_name,
                attempt_count=1,
            ),
        )
