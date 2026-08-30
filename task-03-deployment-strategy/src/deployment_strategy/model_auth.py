"""Short-lived Google identity authentication for a private model plane."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import cast
from urllib.parse import urlsplit

import httpx
from google.auth.transport.requests import Request
from google.oauth2.id_token import fetch_id_token

TokenFetcher = Callable[[str], str]


def _fetch_token(audience: str) -> str:
    return cast(str, fetch_id_token(Request(), audience))  # type: ignore[no-untyped-call]


class GoogleIdTokenAuth(httpx.Auth):
    """Attach a newly fetched ADC ID token to each async model request."""

    def __init__(
        self, audience: str, *, token_fetcher: TokenFetcher = _fetch_token
    ) -> None:
        parsed = urlsplit(audience)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or audience.rstrip("/") != audience
        ):
            raise ValueError("model audience must be a clean HTTPS origin")
        self._audience = audience
        self._token_fetcher = token_fetcher

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await asyncio.to_thread(self._token_fetcher, self._audience)
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 16_384
            or any(character.isspace() or ord(character) < 33 for character in token)
        ):
            raise ValueError("Google identity provider returned an invalid token")
        request.headers["Authorization"] = f"Bearer {token}"
        yield request
