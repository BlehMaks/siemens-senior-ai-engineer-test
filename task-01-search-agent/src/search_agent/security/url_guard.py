"""SSRF-safe URL normalization and just-in-time DNS validation."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from .site_policy import (
    PolicyReason,
    PolicyViolationError,
    SiteDecision,
    SitePolicy,
)

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_EXPLICITLY_NON_PUBLIC_NETWORKS = (ipaddress.IPv4Network("192.88.99.0/24"),)
_LOCAL_HOST_NAMES = frozenset({"home.arpa", "localdomain", "localhost"})
_LOCAL_HOST_SUFFIXES = (
    ".home.arpa",
    ".internal",
    ".lan",
    ".local",
    ".localhost",
    ".localdomain",
)


class HostResolver(Protocol):
    async def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class SystemResolver:
    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return tuple(str(record[4][0]) for record in records)


@dataclass(frozen=True, slots=True)
class GuardedUrl:
    canonical_url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[IpAddress, ...]
    site_decision: SiteDecision


@dataclass(frozen=True, slots=True)
class UrlGuard:
    policy: SitePolicy = field(default_factory=SitePolicy)
    resolver: HostResolver = field(default_factory=SystemResolver)

    async def validate_for_connection(
        self, raw_url: str, *, redirect_count: int = 0
    ) -> GuardedUrl:
        """Revalidate a URL and every resolved address immediately before connect."""

        if (
            isinstance(redirect_count, bool)
            or not isinstance(redirect_count, int)
            or redirect_count < 0
        ):
            raise ValueError("redirect_count must be a non-negative integer")
        if redirect_count > self.policy.max_redirects:
            raise PolicyViolationError(
                PolicyReason.TOO_MANY_REDIRECTS, "redirect limit exceeded"
            )

        scheme, host, port, canonical_url = _normalize_url(raw_url)
        if port not in self.policy.allowed_ports:
            raise PolicyViolationError(
                PolicyReason.DISALLOWED_PORT, "URL port is denied by policy"
            )
        literal = _parse_ip(host)
        addresses: tuple[IpAddress, ...]
        site_decision = self.policy.require_allowed(host)
        if literal is not None:
            addresses = (literal,)
        else:
            if host in _LOCAL_HOST_NAMES or host.endswith(_LOCAL_HOST_SUFFIXES):
                raise PolicyViolationError(
                    PolicyReason.BLOCKED_ADDRESS, "local host names are not allowed"
                )
            try:
                raw_addresses = await self.resolver.resolve(host, port)
            except OSError as exc:
                raise PolicyViolationError(
                    PolicyReason.DNS_FAILURE, "host resolution failed"
                ) from exc
            if not raw_addresses:
                raise PolicyViolationError(
                    PolicyReason.DNS_FAILURE, "host resolution returned no addresses"
                )
            try:
                addresses = tuple(
                    sorted(
                        {ipaddress.ip_address(value) for value in raw_addresses},
                        key=lambda item: (item.version, int(item)),
                    )
                )
            except ValueError as exc:
                raise PolicyViolationError(
                    PolicyReason.DNS_FAILURE,
                    "host resolution returned an invalid address",
                ) from exc

        # Every answer must be public. Accepting one public result beside one private
        # result would leave address selection to the HTTP stack and reopen SSRF.
        if any(not _is_public_address(address) for address in addresses):
            raise PolicyViolationError(
                PolicyReason.BLOCKED_ADDRESS, "URL resolves to a non-public address"
            )
        # Apply canonical IP rules after resolution so numeric and DNS aliases
        # cannot bypass an address explicitly denied by site policy.
        for address in addresses:
            self.policy.require_allowed(address.compressed)
        return GuardedUrl(
            canonical_url=canonical_url,
            scheme=scheme,
            host=host,
            port=port,
            addresses=addresses,
            site_decision=site_decision,
        )

    async def validate_redirect_for_connection(
        self,
        current_url: str,
        location: str,
        *,
        redirect_count: int,
    ) -> GuardedUrl:
        _validate_raw_url_text(location)
        return await self.validate_for_connection(
            urljoin(current_url, location), redirect_count=redirect_count
        )


def _normalize_url(raw_url: str) -> tuple[str, str, int, str]:
    _validate_raw_url_text(raw_url)
    try:
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise PolicyViolationError(
                PolicyReason.DISALLOWED_SCHEME, "URL scheme is not allowed"
            )
        if (
            "@" in parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise PolicyViolationError(
                PolicyReason.CREDENTIALS_IN_URL,
                "credential-bearing URLs are not allowed",
            )
        if parsed.netloc.endswith(":"):
            raise PolicyViolationError(PolicyReason.INVALID_URL, "URL is invalid")
        raw_host = parsed.hostname
        parsed_port = parsed.port
        port = (
            parsed_port
            if parsed_port is not None
            else (443 if scheme == "https" else 80)
        )
    except PolicyViolationError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise PolicyViolationError(PolicyReason.INVALID_URL, "URL is invalid") from exc
    if raw_host is None:
        raise PolicyViolationError(PolicyReason.INVALID_HOST, "URL host is missing")

    host = _normalize_host(raw_host)
    path = parsed.path or "/"
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    netloc = rendered_host if port == default_port else f"{rendered_host}:{port}"
    canonical_url = urlunsplit((scheme, netloc, path, parsed.query, ""))
    return scheme, host, port, canonical_url


def _validate_raw_url_text(raw_url: str) -> None:
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > 2_048:
        raise PolicyViolationError(PolicyReason.INVALID_URL, "URL is invalid")
    if "\\" in raw_url or any(
        character.isspace() or unicodedata.category(character) in {"Cc", "Cf"}
        for character in raw_url
    ):
        raise PolicyViolationError(PolicyReason.INVALID_URL, "URL is invalid")


def _normalize_host(raw_host: str) -> str:
    candidate = raw_host.rstrip(".").casefold()
    if not candidate or "%" in candidate:
        raise PolicyViolationError(PolicyReason.INVALID_HOST, "URL host is invalid")
    literal = _parse_ip(candidate)
    if literal is not None:
        return literal.compressed
    try:
        ascii_host = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise PolicyViolationError(
            PolicyReason.INVALID_HOST, "URL host is invalid"
        ) from exc
    labels = ascii_host.split(".")
    if len(ascii_host) > 253 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise PolicyViolationError(PolicyReason.INVALID_HOST, "URL host is invalid")
    return ascii_host.lower()


def _parse_ip(host: str) -> IpAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_public_address(address: IpAddress) -> bool:
    if any(address in network for network in _EXPLICITLY_NON_PUBLIC_NETWORKS):
        return False
    if (
        not address.is_global
        or address.is_link_local
        or address.is_loopback
        or address.is_multicast
        or address.is_private
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Transition forms can hide an IPv4 destination from a superficial IPv6 check.
        if address.ipv4_mapped is not None or address.sixtofour is not None:
            return False
        if address.teredo is not None:
            return False
        interface_prefix = (int(address) >> 32) & 0xFFFFFFFF
        if interface_prefix in {0x00005EFE, 0x02005EFE}:
            return False
        if address in _NAT64_WELL_KNOWN:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
            return embedded.is_global
        if address in _NAT64_LOCAL:
            return False
    return True
