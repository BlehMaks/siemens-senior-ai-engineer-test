from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)
ProviderRole = Literal["system", "user", "assistant"]
ThinkMode = bool | Literal["low", "medium", "high"] | None


class ProviderMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: ProviderRole
    content: str


class ProviderMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_name: str
    model_name: str
    attempt_count: int
    done_reason: str | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None
    total_duration_ns: int | None = None
    load_duration_ns: int | None = None


@dataclass(frozen=True)
class ProviderResult:
    response: BaseModel
    metadata: ProviderMetadata


@dataclass(frozen=True)
class ProviderInvocation:
    messages: tuple[ProviderMessage, ...]
    schema_name: str
    temperature: float


class ProviderError(RuntimeError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderTransportError(ProviderError):
    pass


class ProviderResponseError(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        metadata: ProviderMetadata | None = None,
    ) -> None:
        super().__init__(message)
        self.metadata = metadata


@dataclass
class BaseScriptedProvider:
    calls: list[ProviderInvocation] = field(default_factory=list)


class StructuredChatProvider(Protocol):
    async def generate_structured(
        self,
        *,
        messages: tuple[ProviderMessage, ...],
        response_model: type[ResponseModelT],
        temperature: float = 0.0,
    ) -> ProviderResult: ...
