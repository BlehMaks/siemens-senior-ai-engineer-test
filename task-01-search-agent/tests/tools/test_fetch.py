from __future__ import annotations

import asyncio
import gzip
import zlib
from collections.abc import AsyncIterator

import httpx
import httpx._decoders as httpx_decoders
import pytest

from search_agent.security import PolicyReason, SitePolicy, UrlGuard
from search_agent.tools.fetch import (
    FetchError,
    FetchFailureReason,
    GuardedFetcher,
    create_fetch_client,
)


class SequencedResolver:
    def __init__(self, answers: dict[str, list[tuple[str, ...]]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        self.calls.append((host, port))
        choices = self.answers[host]
        return choices.pop(0) if len(choices) > 1 else choices[0]


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class TimeoutStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"partial"
        raise httpx.ReadTimeout("slow body")


class ExplodingStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise RuntimeError("transport-secret-must-not-escape")
        yield b"unreachable"  # pragma: no cover


class CloseFailureStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"body"

    async def aclose(self) -> None:
        raise httpx.CloseError("transport-secret-must-not-escape")


class CancellationStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise asyncio.CancelledError
        yield b"unreachable"  # pragma: no cover

    async def aclose(self) -> None:
        self.closed = True


def _guard(
    answers: dict[str, list[tuple[str, ...]]],
    *,
    policy: SitePolicy | None = None,
) -> tuple[UrlGuard, SequencedResolver]:
    resolver = SequencedResolver(answers)
    return UrlGuard(policy=policy or SitePolicy(), resolver=resolver), resolver


@pytest.mark.asyncio
async def test_pins_validated_ip_and_preserves_host_sni_and_logical_url() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content="<p>Grüße from Siemens</p>".encode(),
        )

    guard, resolver = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=0.5
    ) as client:
        document = await GuardedFetcher(client, guard).fetch(
            "HTTPS://EXAMPLE.COM/report#section"
        )
        assert not client.is_closed

    request = seen[0]
    assert request.url == httpx.URL("https://93.184.216.34/report")
    assert request.headers["host"] == "example.com"
    assert request.headers["user-agent"] == "SiemensResearchAgent/0.1"
    assert request.extensions["sni_hostname"] == "example.com"
    assert document.canonical_url == "https://example.com/report"
    assert document.content_type == "text/html"
    assert document.body == "<p>Grüße from Siemens</p>".encode()
    assert resolver.calls == [("example.com", 443)]


@pytest.mark.asyncio
async def test_redirects_are_manual_and_each_hop_is_revalidated() -> None:
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers["host"]))
        if request.url.host == "93.184.216.34":
            return httpx.Response(
                302, headers={"Location": "https://final.example/next"}
            )
        return httpx.Response(
            200, headers={"Content-Type": "text/plain"}, content=b"final body"
        )

    guard, resolver = _guard(
        {
            "start.example": [("93.184.216.34",)],
            "final.example": [("1.1.1.1",)],
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        document = await GuardedFetcher(client, guard).fetch(
            "https://start.example/original"
        )

    assert seen == [
        ("93.184.216.34", "start.example"),
        ("1.1.1.1", "final.example"),
    ]
    assert resolver.calls == [("start.example", 443), ("final.example", 443)]
    assert document.canonical_url == "https://final.example/next"


@pytest.mark.asyncio
async def test_redirect_revalidation_blocks_dns_rebinding_before_second_request() -> (
    None
):
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(302, headers={"Location": "/again"})

    guard, resolver = _guard({"example.com": [("93.184.216.34",), ("127.0.0.1",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as error:
            await GuardedFetcher(client, guard).fetch("https://example.com/start")

    assert error.value.reason is FetchFailureReason.POLICY_REJECTED
    assert error.value.policy_reason is PolicyReason.BLOCKED_ADDRESS
    assert requests == 1
    assert resolver.calls == [("example.com", 443), ("example.com", 443)]


@pytest.mark.asyncio
async def test_redirect_counter_stops_chain_at_policy_limit() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(302, headers={"Location": f"/{requests}"})

    guard, _ = _guard(
        {"example.com": [("93.184.216.34",)]},
        policy=SitePolicy(max_redirects=1),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as error:
            await GuardedFetcher(client, guard).fetch("https://example.com/start")

    assert error.value.reason is FetchFailureReason.TOO_MANY_REDIRECTS
    assert error.value.policy_reason is PolicyReason.TOO_MANY_REDIRECTS
    assert requests == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Location": "http://"},
        [("Location", "/one"), ("Location", "/two")],
    ],
)
async def test_malformed_redirects_are_typed(
    headers: dict[str, str] | list[tuple[str, str]],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers=headers)

    guard, _ = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as error:
            await GuardedFetcher(client, guard).fetch("https://example.com/start")

    assert error.value.reason is FetchFailureReason.MALFORMED_REDIRECT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            httpx.Response(503, headers={"Content-Type": "text/plain"}),
            FetchFailureReason.HTTP_STATUS,
        ),
        (
            httpx.Response(200, headers={"Content-Type": "application/pdf"}),
            FetchFailureReason.CONTENT_TYPE,
        ),
        (
            httpx.Response(
                200,
                headers={"Content-Type": "text/plain", "Content-Length": "6"},
                content=b"abcdef",
            ),
            FetchFailureReason.CONTENT_TOO_LARGE,
        ),
        (
            httpx.Response(
                200,
                headers={"Content-Type": "text/plain", "Content-Length": "nope"},
                content=b"content",
            ),
            FetchFailureReason.INVALID_RESPONSE,
        ),
        (
            httpx.Response(200, headers={"Content-Type": "text/plain"}),
            FetchFailureReason.EMPTY_CONTENT,
        ),
    ],
)
async def test_status_content_type_length_and_empty_failures_are_typed(
    response: httpx.Response,
    reason: FetchFailureReason,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    guard, _ = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as error:
            await GuardedFetcher(client, guard, max_bytes=5).fetch(
                "https://example.com"
            )

    assert error.value.reason is reason
    if reason is FetchFailureReason.HTTP_STATUS:
        assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_streamed_body_is_capped_without_content_length() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            stream=ChunkStream((b"abc", b"def")),
        )

    guard, _ = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as error:
            await GuardedFetcher(client, guard, max_bytes=5).fetch(
                "https://example.com"
            )

    assert error.value.reason is FetchFailureReason.CONTENT_TOO_LARGE


@pytest.mark.asyncio
async def test_decompressed_body_is_capped_and_valid_gzip_is_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpx_decoder_calls = 0
    original_decode = httpx_decoders.GZipDecoder.decode

    def tracking_decode(decoder: httpx_decoders.GZipDecoder, data: bytes) -> bytes:
        nonlocal httpx_decoder_calls
        httpx_decoder_calls += 1
        return original_decode(decoder, data)

    monkeypatch.setattr(httpx_decoders.GZipDecoder, "decode", tracking_decode)
    large = gzip.compress(b"x" * 1_000)
    small = gzip.compress(b"decoded text")
    payloads = iter((large, small))

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = next(payloads)
        return httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(payload)),
                "Content-Type": "text/plain",
            },
            stream=ChunkStream((payload,)),
        )

    guard, _ = _guard({"example.com": [("93.184.216.34",), ("93.184.216.34",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GuardedFetcher(client, guard, max_bytes=100)
        with pytest.raises(FetchError) as error:
            await fetcher.fetch("https://example.com/large")
        document = await fetcher.fetch("https://example.com/small")

    assert error.value.reason is FetchFailureReason.CONTENT_TOO_LARGE
    assert document.body == b"decoded text"
    assert httpx_decoder_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "payload"),
    [
        ("deflate", zlib.compress(b"deflate body")),
        ("identity", b"identity body"),
    ],
)
async def test_supported_content_encodings_are_decoded(
    encoding: str, payload: bytes
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": encoding, "Content-Type": "text/plain"},
            stream=ChunkStream((payload,)),
        )

    guard, _ = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        document = await GuardedFetcher(client, guard).fetch("https://example.com")

    assert document.body == f"{encoding} body".encode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "payload"),
    [
        ("gzip", gzip.compress(b"truncated")[:-4]),
        ("br", b"unsupported"),
    ],
)
async def test_truncated_and_unsupported_compression_are_typed(
    encoding: str, payload: bytes
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": encoding, "Content-Type": "text/plain"},
            stream=ChunkStream((payload,)),
        )

    guard, _ = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as error:
            await GuardedFetcher(client, guard).fetch("https://example.com")

    assert error.value.reason is FetchFailureReason.INVALID_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream", "headers", "reason"),
    [
        (
            TimeoutStream(),
            {"Content-Type": "text/plain"},
            FetchFailureReason.TIMEOUT,
        ),
        (
            ChunkStream((b"not-gzip",)),
            {"Content-Type": "text/plain", "Content-Encoding": "gzip"},
            FetchFailureReason.INVALID_RESPONSE,
        ),
        (
            ExplodingStream(),
            {"Content-Type": "text/plain"},
            FetchFailureReason.NETWORK_ERROR,
        ),
    ],
)
async def test_body_timeout_and_invalid_compression_are_typed(
    stream: httpx.AsyncByteStream,
    headers: dict[str, str],
    reason: FetchFailureReason,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=stream)

    guard, _ = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as error:
            await GuardedFetcher(client, guard).fetch("https://example.com")

    assert error.value.reason is reason
    assert "transport-secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "network", "unknown"])
async def test_timeout_and_network_failures_are_typed(failure: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("slow response", request=request)
        if failure == "network":
            raise httpx.ConnectError("connection failed", request=request)
        raise RuntimeError("transport-secret-must-not-escape")

    guard, _ = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as error:
            await GuardedFetcher(client, guard).fetch("https://example.com")

    expected = (
        FetchFailureReason.TIMEOUT
        if failure == "timeout"
        else FetchFailureReason.NETWORK_ERROR
    )
    assert error.value.reason is expected
    assert "transport-secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "reason"),
    [
        (503, FetchFailureReason.HTTP_STATUS),
        (200, FetchFailureReason.NETWORK_ERROR),
    ],
)
async def test_close_failure_is_sanitized_without_masking_primary_failure(
    status_code: int, reason: FetchFailureReason
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"Content-Type": "text/plain"},
            stream=CloseFailureStream(),
        )

    guard, _ = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as error:
            await GuardedFetcher(client, guard).fetch("https://example.com")

    assert error.value.reason is reason
    assert "transport-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_body_cancellation_propagates_after_response_is_closed() -> None:
    stream = CancellationStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            stream=stream,
        )

    guard, _ = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.CancelledError):
            await GuardedFetcher(client, guard).fetch("https://example.com")

    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"max_bytes": True},
        {"user_agent": ""},
        {"user_agent": "unsafe\r\nheader"},
    ],
)
async def test_fetcher_rejects_invalid_limits(kwargs: dict[str, object]) -> None:
    guard, _ = _guard({"example.com": [("93.184.216.34",)]})
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            GuardedFetcher(client, guard, **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_client_factory_uses_bounded_configuration() -> None:
    client = create_fetch_client(
        connect_timeout=1.0,
        read_timeout=2.0,
        write_timeout=3.0,
        pool_timeout=4.0,
        max_connections=2,
    )
    try:
        assert client.follow_redirects is False
        assert client.timeout.connect == 1.0
        assert client.timeout.read == 2.0
        assert client.timeout.write == 3.0
        assert client.timeout.pool == 4.0
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"connect_timeout": 0.0},
        {"read_timeout": float("inf")},
        {"write_timeout": True},
        {"max_connections": 0},
    ],
)
def test_client_factory_rejects_invalid_configuration(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        create_fetch_client(**kwargs)  # type: ignore[arg-type]
