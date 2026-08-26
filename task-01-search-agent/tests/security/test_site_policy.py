from __future__ import annotations

import pytest

from search_agent import (
    PolicyReason,
    PolicyViolationError,
    SafeSearch,
    SiteCategory,
    SitePolicy,
)


def test_default_policy_is_strict_and_blocks_metadata_names() -> None:
    policy = SitePolicy()

    assert policy.safe_search is SafeSearch.STRICT
    assert policy.evaluate("example.com").allowed is True
    assert (
        policy.evaluate("metadata.google.internal").reason is PolicyReason.DENIED_DOMAIN
    )
    with pytest.raises(PolicyViolationError) as error:
        policy.require_allowed("sub.metadata.google.internal")
    assert error.value.reason is PolicyReason.DENIED_DOMAIN


def test_domain_precedence_is_deny_then_allow_then_category() -> None:
    categories = {
        "example.com": SiteCategory.ADULT,
        "social.example": SiteCategory.SOCIAL,
    }
    policy = SitePolicy(
        denied_domains=frozenset({"blocked.docs.example.com"}),
        allowed_domains=frozenset({"docs.example.com"}),
        domain_categories=categories,
        denied_categories=frozenset({SiteCategory.ADULT, SiteCategory.SOCIAL}),
    )
    categories["later.example"] = SiteCategory.MALWARE

    assert policy.evaluate("example.com").reason is PolicyReason.DENIED_CATEGORY
    assert policy.evaluate("api.example.com").category is SiteCategory.ADULT
    assert policy.evaluate("docs.example.com").allowed is True
    assert (
        policy.evaluate("blocked.docs.example.com").reason is PolicyReason.DENIED_DOMAIN
    )
    assert policy.evaluate("notexample.com").allowed is True
    assert policy.evaluate("later.example").category is None


def test_policy_domains_are_idna_normalized_and_subdomain_safe() -> None:
    policy = SitePolicy(denied_domains=frozenset({"BÜCHER.example."}))

    assert policy.evaluate("xn--bcher-kva.example").reason is PolicyReason.DENIED_DOMAIN
    assert (
        policy.evaluate("shop.xn--bcher-kva.example").reason
        is PolicyReason.DENIED_DOMAIN
    )
    assert policy.evaluate("notxn--bcher-kva.example").allowed is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"denied_domains": frozenset({"https://example.com"})},
        {"allowed_ports": frozenset()},
        {"allowed_ports": frozenset({0})},
        {"allowed_ports": frozenset({True})},
        {"max_redirects": -1},
        {"max_redirects": True},
        {"max_redirects": 11},
    ],
)
def test_policy_rejects_invalid_configuration(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SitePolicy(**kwargs)  # type: ignore[arg-type]
