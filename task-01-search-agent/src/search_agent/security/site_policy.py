"""Auditable domain, category, port, redirect, and SafeSearch policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import ip_address
from types import MappingProxyType


class PolicyReason(StrEnum):
    ALLOWED = "allowed"
    INVALID_URL = "invalid_url"
    DISALLOWED_SCHEME = "disallowed_scheme"
    CREDENTIALS_IN_URL = "credentials_in_url"
    INVALID_HOST = "invalid_host"
    DISALLOWED_PORT = "disallowed_port"
    BLOCKED_ADDRESS = "blocked_address"
    DNS_FAILURE = "dns_failure"
    DENIED_DOMAIN = "denied_domain"
    DENIED_CATEGORY = "denied_category"
    TOO_MANY_REDIRECTS = "too_many_redirects"


class SiteCategory(StrEnum):
    ADULT = "adult"
    GAMBLING = "gambling"
    MALWARE = "malware"
    SOCIAL = "social"


class SafeSearch(StrEnum):
    MODERATE = "moderate"
    STRICT = "strict"


class PolicyViolationError(ValueError):
    """A safe public error carrying an auditable policy reason code."""

    def __init__(self, reason: PolicyReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SiteDecision:
    allowed: bool
    reason: PolicyReason
    category: SiteCategory | None = None


_DEFAULT_DENIED_DOMAINS = frozenset(
    {
        "instance-data",
        "metadata",
        "metadata.azure.internal",
        "metadata.google.internal",
    }
)


@dataclass(frozen=True, slots=True)
class SitePolicy:
    """Deny domains first, then explicit allows, then denied categories."""

    denied_domains: frozenset[str] = _DEFAULT_DENIED_DOMAINS
    allowed_domains: frozenset[str] = frozenset()
    domain_categories: Mapping[str, SiteCategory] = field(default_factory=dict)
    denied_categories: frozenset[SiteCategory] = frozenset(
        {SiteCategory.ADULT, SiteCategory.GAMBLING, SiteCategory.MALWARE}
    )
    allowed_ports: frozenset[int] = frozenset({80, 443})
    safe_search: SafeSearch = SafeSearch.STRICT
    max_redirects: int = 3

    def __post_init__(self) -> None:
        denied = frozenset(
            _normalize_policy_domain(item) for item in self.denied_domains
        )
        allowed = frozenset(
            _normalize_policy_domain(item) for item in self.allowed_domains
        )
        categories = {
            _normalize_policy_domain(domain): SiteCategory(category)
            for domain, category in self.domain_categories.items()
        }
        if not self.allowed_ports or any(
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            for port in self.allowed_ports
        ):
            raise ValueError("allowed_ports must contain valid TCP ports")
        if (
            isinstance(self.max_redirects, bool)
            or not isinstance(self.max_redirects, int)
            or not 0 <= self.max_redirects <= 10
        ):
            raise ValueError("max_redirects must be an integer between 0 and 10")
        object.__setattr__(self, "denied_domains", denied)
        object.__setattr__(self, "allowed_domains", allowed)
        object.__setattr__(self, "domain_categories", MappingProxyType(categories))
        object.__setattr__(
            self,
            "denied_categories",
            frozenset(SiteCategory(item) for item in self.denied_categories),
        )
        object.__setattr__(self, "allowed_ports", frozenset(self.allowed_ports))
        object.__setattr__(self, "safe_search", SafeSearch(self.safe_search))

    def evaluate(self, host: str) -> SiteDecision:
        normalized_host = _normalize_policy_domain(host)
        if _most_specific_rule(normalized_host, self.denied_domains) is not None:
            return SiteDecision(False, PolicyReason.DENIED_DOMAIN)
        if _most_specific_rule(normalized_host, self.allowed_domains) is not None:
            return SiteDecision(True, PolicyReason.ALLOWED)

        category_rule = _most_specific_rule(
            normalized_host, frozenset(self.domain_categories)
        )
        category = (
            self.domain_categories[category_rule] if category_rule is not None else None
        )
        if category in self.denied_categories:
            return SiteDecision(False, PolicyReason.DENIED_CATEGORY, category)
        return SiteDecision(True, PolicyReason.ALLOWED, category)

    def require_allowed(self, host: str) -> SiteDecision:
        decision = self.evaluate(host)
        if not decision.allowed:
            raise PolicyViolationError(decision.reason, "site is denied by policy")
        return decision


def _most_specific_rule(host: str, rules: frozenset[str]) -> str | None:
    matches = [rule for rule in rules if host == rule or host.endswith(f".{rule}")]
    return max(matches, key=len) if matches else None


def _normalize_policy_domain(domain: str) -> str:
    if not isinstance(domain, str):
        raise ValueError("policy domains must be strings")
    candidate = domain.strip().rstrip(".").casefold()
    if not candidate:
        raise ValueError("policy domains must be bare host names")
    try:
        return ip_address(candidate).compressed
    except ValueError:
        pass
    if any(character in candidate for character in "/:@%\\"):
        raise ValueError("policy domains must be bare host names")
    try:
        ascii_domain = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("policy domain is not valid IDNA") from exc
    labels = ascii_domain.split(".")
    if len(ascii_domain) > 253 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise ValueError("policy domain is invalid")
    return ascii_domain.lower()
