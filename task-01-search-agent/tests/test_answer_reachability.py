"""Regressions for defects that made ordinary requests unanswerable."""

from __future__ import annotations

import pytest

from search_agent.tools.search import search_text_for


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("what wikipedia says about germany?", "wikipedia germany"),
        ("where can I find mcdonalds in munich?", "find mcdonalds munich"),
        ("Siemens sustainability report 2024", "Siemens sustainability report 2024"),
    ],
)
def test_search_text_keeps_only_topic_words(request_text: str, expected: str) -> None:
    assert search_text_for(request_text) == expected


def test_search_text_stays_a_subset_of_the_request() -> None:
    request = "where can I find mcdonalds in munich?"
    request_words = set(request.lower().replace("?", "").split())

    assert set(search_text_for(request).lower().split()) <= request_words


def test_search_text_survives_an_all_common_word_request() -> None:
    assert search_text_for("what is it?") == "what is it"
