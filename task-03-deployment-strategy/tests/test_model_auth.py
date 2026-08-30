from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from deployment_strategy.model_auth import GoogleIdTokenAuth


@pytest.mark.asyncio
async def test_google_id_token_auth_fetches_a_fresh_token_for_every_request() -> None:
    audiences: list[str] = []
    captured_headers: list[str] = []

    def fetch_token(audience: str) -> str:
        audiences.append(audience)
        return f"token-{len(audiences)}"

    def handle(request: httpx.Request) -> httpx.Response:
        captured_headers.append(request.headers["Authorization"])
        return httpx.Response(200, json={"ok": True})

    audience = "https://private-model.example.run.app"
    async with httpx.AsyncClient(
        auth=GoogleIdTokenAuth(audience, token_fetcher=fetch_token),
        transport=httpx.MockTransport(handle),
    ) as client:
        assert (await client.get(f"{audience}/first")).status_code == 200
        assert (await client.get(f"{audience}/second")).status_code == 200

    assert audiences == [audience, audience]
    assert captured_headers == ["Bearer token-1", "Bearer token-2"]


@pytest.mark.parametrize(
    "audience",
    [
        "http://private-model.example.run.app",
        "https://user@private-model.example.run.app",
        "https://private-model.example.run.app/path",
        "https://private-model.example.run.app?query=1",
        "https://private-model.example.run.app/",
    ],
)
def test_google_id_token_auth_rejects_non_origin_audiences(audience: str) -> None:
    with pytest.raises(ValueError, match="clean HTTPS origin"):
        GoogleIdTokenAuth(audience)


@pytest.mark.asyncio
async def test_google_id_token_auth_rejects_an_invalid_token() -> None:
    def token_fetcher(_audience: str) -> str:
        return "bad token"

    checked_fetcher: Callable[[str], str] = token_fetcher
    auth = GoogleIdTokenAuth(
        "https://private-model.example.run.app", token_fetcher=checked_fetcher
    )

    async with httpx.AsyncClient(
        auth=auth,
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
    ) as client:
        with pytest.raises(ValueError, match="invalid token"):
            await client.get("https://private-model.example.run.app/api/chat")
