from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from search_agent import (
    PolicyReason,
    PolicyViolationError,
    SitePolicy,
    UrlGuard,
)


@dataclass
class FakeResolver:
    answers: dict[str, tuple[str, ...]]
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        self.calls.append((host, port))
        answer = self.answers.get(host)
        if answer is None:
            raise OSError("not found")
        return answer


@dataclass
class RebindingResolver:
    answers: list[tuple[str, ...]]

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        del host, port
        return self.answers.pop(0)


def _guard(
    answers: dict[str, tuple[str, ...]], *, policy: SitePolicy | None = None
) -> UrlGuard:
    return UrlGuard(policy=policy or SitePolicy(), resolver=FakeResolver(answers))


@pytest.mark.asyncio
async def test_normalizes_idna_default_port_and_fragment() -> None:
    resolver = FakeResolver({"xn--bcher-kva.example": ("93.184.216.34",)})
    guard = UrlGuard(resolver=resolver)

    result = await guard.validate_for_connection(
        "HTTPS://BÜCHER.example.:443/report?q=1#section"
    )

    assert result.canonical_url == "https://xn--bcher-kva.example/report?q=1"
    assert result.host == "xn--bcher-kva.example"
    assert result.port == 443
    assert tuple(str(address) for address in result.addresses) == ("93.184.216.34",)
    assert resolver.calls == [("xn--bcher-kva.example", 443)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", PolicyReason.DISALLOWED_SCHEME),
        ("https://user@example.com/", PolicyReason.CREDENTIALS_IN_URL),
        ("https://example.com:22/", PolicyReason.DISALLOWED_PORT),
        ("https://127%2e0%2e0%2e1/", PolicyReason.INVALID_HOST),
        ("https://example.com\\@127.0.0.1/", PolicyReason.INVALID_URL),
        (" https://example.com/", PolicyReason.INVALID_URL),
        ("https://exam\u200bple.com/", PolicyReason.INVALID_URL),
        ("https://example.com:/", PolicyReason.INVALID_URL),
        ("https://example.com:99999/", PolicyReason.INVALID_URL),
        ("https:///missing-host", PolicyReason.INVALID_HOST),
    ],
)
async def test_rejects_malformed_or_disallowed_urls(
    url: str, reason: PolicyReason
) -> None:
    with pytest.raises(PolicyViolationError) as error:
        await _guard({"example.com": ("93.184.216.34",)}).validate_for_connection(url)

    assert error.value.reason is reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "64:ff9b:1::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
    ],
)
async def test_blocks_every_non_public_literal_address(address: str) -> None:
    rendered = f"[{address}]" if ":" in address else address

    with pytest.raises(PolicyViolationError) as error:
        await _guard({}).validate_for_connection(f"https://{rendered}/")

    assert error.value.reason is PolicyReason.BLOCKED_ADDRESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://8.8.8.8/path", "8.8.8.8"),
        ("https://[2606:4700:4700::1111]/", "2606:4700:4700::1111"),
    ],
)
async def test_allows_public_ip_literals(url: str, expected: str) -> None:
    result = await _guard({}).validate_for_connection(url)

    assert str(result.addresses[0]) == expected


@pytest.mark.asyncio
async def test_validates_all_dns_answers_and_alternate_numeric_hosts() -> None:
    mixed = _guard({"example.com": ("93.184.216.34", "127.0.0.1")})
    alternate = _guard({"2130706433": ("127.0.0.1",)})

    for guard, url in (
        (mixed, "https://example.com"),
        (alternate, "https://2130706433"),
    ):
        with pytest.raises(PolicyViolationError) as error:
            await guard.validate_for_connection(url)
        assert error.value.reason is PolicyReason.BLOCKED_ADDRESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [(), ("not-an-ip",)],
)
async def test_maps_empty_or_invalid_dns_answers_to_reason_code(
    answers: tuple[str, ...],
) -> None:
    with pytest.raises(PolicyViolationError) as error:
        await _guard({"example.com": answers}).validate_for_connection(
            "https://example.com"
        )

    assert error.value.reason is PolicyReason.DNS_FAILURE


@pytest.mark.asyncio
async def test_denied_and_local_names_fail_before_connection() -> None:
    resolver = FakeResolver(
        {
            "metadata.google.internal": ("8.8.8.8",),
            "home.arpa": ("8.8.8.8",),
            "service.local": ("8.8.8.8",),
        }
    )
    guard = UrlGuard(resolver=resolver)

    with pytest.raises(PolicyViolationError) as metadata_error:
        await guard.validate_for_connection("https://metadata.google.internal")
    with pytest.raises(PolicyViolationError) as local_error:
        await guard.validate_for_connection("https://service.local")
    with pytest.raises(PolicyViolationError) as home_arpa_error:
        await guard.validate_for_connection("https://home.arpa")

    assert metadata_error.value.reason is PolicyReason.DENIED_DOMAIN
    assert local_error.value.reason is PolicyReason.BLOCKED_ADDRESS
    assert home_arpa_error.value.reason is PolicyReason.BLOCKED_ADDRESS
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_each_redirect_is_joined_revalidated_and_bounded() -> None:
    policy = SitePolicy(
        denied_domains=frozenset({"blocked.example"}),
        max_redirects=1,
    )
    guard = _guard(
        {
            "example.com": ("93.184.216.34",),
            "blocked.example": ("93.184.216.34",),
        },
        policy=policy,
    )

    relative = await guard.validate_redirect_for_connection(
        "https://example.com/start", "/next", redirect_count=1
    )
    assert relative.canonical_url == "https://example.com/next"

    with pytest.raises(PolicyViolationError) as denied:
        await guard.validate_redirect_for_connection(
            "https://example.com/start",
            "//blocked.example/path",
            redirect_count=1,
        )
    with pytest.raises(PolicyViolationError) as exhausted:
        await guard.validate_redirect_for_connection(
            "https://example.com/start", "/third", redirect_count=2
        )
    assert denied.value.reason is PolicyReason.DENIED_DOMAIN
    assert exhausted.value.reason is PolicyReason.TOO_MANY_REDIRECTS


@pytest.mark.asyncio
async def test_revalidation_detects_dns_rebinding() -> None:
    guard = UrlGuard(
        resolver=RebindingResolver([("93.184.216.34",), ("169.254.169.254",)])
    )

    first = await guard.validate_for_connection("https://example.com")
    assert str(first.addresses[0]) == "93.184.216.34"

    with pytest.raises(PolicyViolationError) as error:
        await guard.validate_for_connection("https://example.com")
    assert error.value.reason is PolicyReason.BLOCKED_ADDRESS
