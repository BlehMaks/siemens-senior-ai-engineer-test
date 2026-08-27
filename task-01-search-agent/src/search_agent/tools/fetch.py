"""Bounded HTTP retrieval that connects only to URL-guarded addresses."""

from __future__ import annotations

import sys
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from numbers import Real
from typing import NoReturn, Protocol, cast

import httpx

from ..security import (
    GuardedUrl,
    PolicyReason,
    PolicyViolationError,
    UrlGuard,
)

_ALLOWED_CONTENT_TYPES = frozenset({"application/xhtml+xml", "text/html", "text/plain"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DECODE_CHUNK_BYTES = 64 * 1024
_MALFORMED_URL_REASONS = frozenset(
    {PolicyReason.INVALID_URL, PolicyReason.INVALID_HOST}
)


class _Decompressor(Protocol):
    eof: bool
    unconsumed_tail: bytes
    unused_data: bytes

    def decompress(self, data: bytes, max_length: int = 0) -> bytes: ...


class FetchFailureReason(StrEnum):
    POLICY_REJECTED = "policy_rejected"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    MALFORMED_REDIRECT = "malformed_redirect"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    HTTP_STATUS = "http_status"
    CONTENT_TYPE = "content_type"
    CONTENT_TOO_LARGE = "content_too_large"
    EMPTY_CONTENT = "empty_content"
    INVALID_RESPONSE = "invalid_response"


class FetchError(RuntimeError):
    """Safe, typed retrieval failure without response-body disclosure."""

    def __init__(
        self,
        reason: FetchFailureReason,
        message: str,
        *,
        status_code: int | None = None,
        policy_reason: PolicyReason | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code
        self.policy_reason = policy_reason


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    canonical_url: str
    content_type: str
    body: bytes


def _validated_fetched_document(value: object) -> FetchedDocument:
    """Copy a fetched value with base operations before byte accounting or parsing."""

    if (
        type(value) is not FetchedDocument
        or type(value.canonical_url) is not str
        or type(value.content_type) is not str
        or not isinstance(value.body, bytes)
    ):
        raise TypeError("fetch port returned an invalid document")
    body = bytes.__getitem__(value.body, slice(None))
    if type(body) is not bytes:
        raise TypeError("fetch port returned an invalid body")
    return FetchedDocument(
        canonical_url=value.canonical_url,
        content_type=value.content_type,
        body=body,
    )


def create_fetch_client(
    *,
    connect_timeout: float = 5.0,
    read_timeout: float = 10.0,
    write_timeout: float = 5.0,
    pool_timeout: float = 5.0,
    max_connections: int = 10,
) -> httpx.AsyncClient:
    """Create the bounded shared client expected by ``GuardedFetcher``."""

    timeout_values = (connect_timeout, read_timeout, write_timeout, pool_timeout)
    if any(
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not isfinite(float(value))
        or value <= 0
        for value in timeout_values
    ):
        raise ValueError("fetch timeouts must be positive finite numbers")
    if (
        isinstance(max_connections, bool)
        or not isinstance(max_connections, int)
        or max_connections <= 0
    ):
        raise ValueError("max_connections must be a positive integer")
    return httpx.AsyncClient(
        follow_redirects=False,
        http2=False,
        trust_env=False,
        limits=httpx.Limits(
            max_connections=max_connections,
            # Pinned IP origins must not reuse a TLS session across logical hosts.
            max_keepalive_connections=0,
        ),
        timeout=httpx.Timeout(
            connect=float(connect_timeout),
            read=float(read_timeout),
            write=float(write_timeout),
            pool=float(pool_timeout),
        ),
    )


@dataclass(slots=True)
class GuardedFetcher:
    """Use an injected shared client; its lifecycle belongs to the caller."""

    client: httpx.AsyncClient
    guard: UrlGuard
    max_bytes: int = 2 * 1024 * 1024
    user_agent: str = "SiemensResearchAgent/0.1"

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or self.max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        if (
            not isinstance(self.user_agent, str)
            or not self.user_agent.strip()
            or len(self.user_agent) > 200
            or not self.user_agent.isascii()
            or any(not 32 <= ord(character) < 127 for character in self.user_agent)
        ):
            raise ValueError("user_agent must be printable ASCII")

    async def fetch(self, raw_url: str) -> FetchedDocument:
        guarded = await self._validate_initial(raw_url)
        redirect_count = 0

        while True:
            response = await self._send(guarded)
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = _redirect_location(response)
                else:
                    body, content_type = await self._read_success(response)
                    return FetchedDocument(
                        canonical_url=guarded.canonical_url,
                        content_type=content_type,
                        body=body,
                    )
            finally:
                await _close_response(
                    response, suppress_failure=sys.exception() is not None
                )

            redirect_count += 1
            guarded = await self._validate_redirect(
                guarded.canonical_url,
                location,
                redirect_count=redirect_count,
            )

    async def _validate_initial(self, raw_url: str) -> GuardedUrl:
        try:
            return await self.guard.validate_for_connection(raw_url)
        except PolicyViolationError as exc:
            _raise_policy_failure(exc, redirect=False)

    async def _validate_redirect(
        self,
        current_url: str,
        location: str,
        *,
        redirect_count: int,
    ) -> GuardedUrl:
        try:
            # Revalidation happens here, immediately before building the next request.
            return await self.guard.validate_redirect_for_connection(
                current_url,
                location,
                redirect_count=redirect_count,
            )
        except PolicyViolationError as exc:
            _raise_policy_failure(exc, redirect=True)

    async def _send(self, guarded: GuardedUrl) -> httpx.Response:
        request = _pinned_request(guarded, user_agent=self.user_agent)
        try:
            return await self.client.send(
                request,
                stream=True,
                auth=None,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise FetchError(FetchFailureReason.TIMEOUT, "fetch timed out") from None
        except httpx.DecodingError:
            raise FetchError(
                FetchFailureReason.INVALID_RESPONSE,
                "response compression is invalid",
            ) from None
        except httpx.RequestError:
            raise FetchError(
                FetchFailureReason.NETWORK_ERROR, "fetch transport failed"
            ) from None
        except Exception:
            raise FetchError(
                FetchFailureReason.NETWORK_ERROR, "fetch transport failed"
            ) from None

    async def _read_success(self, response: httpx.Response) -> tuple[bytes, str]:
        if not 200 <= response.status_code < 300:
            raise FetchError(
                FetchFailureReason.HTTP_STATUS,
                f"fetch returned HTTP {response.status_code}",
                status_code=response.status_code,
            )

        content_type = _content_type(response)
        declared_length = _content_length(response)
        if declared_length is not None and declared_length > self.max_bytes:
            raise FetchError(
                FetchFailureReason.CONTENT_TOO_LARGE,
                "response exceeds the byte limit",
            )

        encoding = _content_encoding(response)
        if response.is_stream_consumed and encoding != "identity":
            raise FetchError(
                FetchFailureReason.INVALID_RESPONSE,
                "response was decoded before bounded retrieval",
            )
        decoder = _decoder(encoding)
        body = bytearray()
        raw_size = 0
        try:
            # Raw streaming keeps third-party decoders from materializing a zip bomb.
            async for chunk in _raw_chunks(response):
                raw_size += len(chunk)
                if raw_size > self.max_bytes:
                    raise FetchError(
                        FetchFailureReason.CONTENT_TOO_LARGE,
                        "response exceeds the byte limit",
                    )
                if decoder is None:
                    _append_bounded(body, chunk, self.max_bytes)
                else:
                    _decode_bounded(decoder, chunk, body, self.max_bytes)
            if decoder is not None and (not decoder.eof or decoder.unused_data):
                raise FetchError(
                    FetchFailureReason.INVALID_RESPONSE,
                    "response compression is invalid",
                )
        except FetchError:
            raise
        except httpx.TimeoutException:
            raise FetchError(FetchFailureReason.TIMEOUT, "fetch timed out") from None
        except httpx.RequestError:
            raise FetchError(
                FetchFailureReason.NETWORK_ERROR, "fetch transport failed"
            ) from None
        except zlib.error:
            raise FetchError(
                FetchFailureReason.INVALID_RESPONSE,
                "response compression is invalid",
            ) from None
        except Exception:
            raise FetchError(
                FetchFailureReason.NETWORK_ERROR, "response stream failed"
            ) from None

        rendered = bytes(body)
        if not rendered.strip():
            raise FetchError(FetchFailureReason.EMPTY_CONTENT, "response body is empty")
        return rendered, content_type


def _pinned_request(guarded: GuardedUrl, *, user_agent: str) -> httpx.Request:
    logical_url = httpx.URL(guarded.canonical_url)
    pinned_url = logical_url.copy_with(host=guarded.addresses[0].compressed)
    host = f"[{guarded.host}]" if ":" in guarded.host else guarded.host
    default_port = 443 if guarded.scheme == "https" else 80
    host_header = host if guarded.port == default_port else f"{host}:{guarded.port}"
    headers = {
        "Accept": "text/html, text/plain;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
        "Host": host_header,
        "User-Agent": user_agent,
    }
    extensions: dict[str, object] = {}
    if guarded.scheme == "https":
        # The socket target stays pinned while TLS verifies the logical host.
        extensions["sni_hostname"] = guarded.host
    return httpx.Request("GET", pinned_url, headers=headers, extensions=extensions)


def _redirect_location(response: httpx.Response) -> str:
    values = response.headers.get_list("location")
    if len(values) != 1 or not values[0]:
        raise FetchError(
            FetchFailureReason.MALFORMED_REDIRECT,
            "redirect must contain one Location header",
        )
    return values[0]


def _content_type(response: httpx.Response) -> str:
    values = response.headers.get_list("content-type")
    if len(values) != 1:
        raise FetchError(
            FetchFailureReason.CONTENT_TYPE,
            "response must declare one supported content type",
        )
    media_type = values[0].partition(";")[0].strip().lower()
    if media_type not in _ALLOWED_CONTENT_TYPES:
        raise FetchError(
            FetchFailureReason.CONTENT_TYPE,
            "response content type is not supported",
        )
    return media_type


def _content_length(response: httpx.Response) -> int | None:
    values = response.headers.get_list("content-length")
    if not values:
        return None
    rendered = values[0].strip() if len(values) == 1 else ""
    if (
        not rendered
        or len(rendered) > 20
        or not rendered.isascii()
        or not rendered.isdigit()
    ):
        raise FetchError(
            FetchFailureReason.INVALID_RESPONSE,
            "response Content-Length is invalid",
        )
    return int(rendered)


def _content_encoding(response: httpx.Response) -> str:
    values = response.headers.get_list("content-encoding")
    if not values:
        return "identity"
    encoding = values[0].strip().lower() if len(values) == 1 else ""
    if encoding not in {"deflate", "gzip", "identity"}:
        raise FetchError(
            FetchFailureReason.INVALID_RESPONSE,
            "response content encoding is not supported",
        )
    return encoding


def _decoder(encoding: str) -> _Decompressor | None:
    if encoding == "gzip":
        return cast(_Decompressor, zlib.decompressobj(zlib.MAX_WBITS | 16))
    if encoding == "deflate":
        return cast(_Decompressor, zlib.decompressobj(zlib.MAX_WBITS))
    return None


async def _raw_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    if hasattr(response, "_content"):
        content = response.content
        for start in range(0, len(content), _DECODE_CHUNK_BYTES):
            yield content[start : start + _DECODE_CHUNK_BYTES]
        return
    async for chunk in response.aiter_raw(chunk_size=_DECODE_CHUNK_BYTES):
        yield chunk


def _append_bounded(body: bytearray, chunk: bytes, max_bytes: int) -> None:
    if len(body) + len(chunk) > max_bytes:
        raise FetchError(
            FetchFailureReason.CONTENT_TOO_LARGE,
            "response exceeds the byte limit",
        )
    body.extend(chunk)


def _decode_bounded(
    decoder: _Decompressor,
    chunk: bytes,
    body: bytearray,
    max_bytes: int,
) -> None:
    pending = chunk
    while pending:
        output_limit = min(_DECODE_CHUNK_BYTES, max_bytes - len(body) + 1)
        decoded = decoder.decompress(pending, output_limit)
        _append_bounded(body, decoded, max_bytes)
        remaining = decoder.unconsumed_tail
        if remaining and not decoded and len(remaining) == len(pending):
            raise FetchError(
                FetchFailureReason.INVALID_RESPONSE,
                "response compression made no progress",
            )
        pending = remaining
    if decoder.unused_data:
        raise FetchError(
            FetchFailureReason.INVALID_RESPONSE,
            "response compression is invalid",
        )


async def _close_response(response: httpx.Response, *, suppress_failure: bool) -> None:
    try:
        await response.aclose()
    except Exception as error:
        if suppress_failure:
            return
        if isinstance(error, httpx.TimeoutException):
            reason = FetchFailureReason.TIMEOUT
            message = "response close timed out"
        elif isinstance(error, httpx.RequestError):
            reason = FetchFailureReason.NETWORK_ERROR
            message = "response close failed"
        else:
            reason = FetchFailureReason.INVALID_RESPONSE
            message = "response could not be closed"
        raise FetchError(reason, message) from None


def _raise_policy_failure(error: PolicyViolationError, *, redirect: bool) -> NoReturn:
    if error.reason is PolicyReason.TOO_MANY_REDIRECTS:
        reason = FetchFailureReason.TOO_MANY_REDIRECTS
    elif redirect and error.reason in _MALFORMED_URL_REASONS:
        reason = FetchFailureReason.MALFORMED_REDIRECT
    else:
        reason = FetchFailureReason.POLICY_REJECTED
    raise FetchError(
        reason,
        "fetch URL was rejected by policy",
        policy_reason=error.reason,
    ) from error
