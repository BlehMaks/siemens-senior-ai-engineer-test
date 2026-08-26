from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
import trafilatura
import trafilatura.downloads

from search_agent.tools import extract as extract_module
from search_agent.tools.extract import (
    ExtractionError,
    ExtractionFailureReason,
    LocalExtractor,
)
from search_agent.tools.fetch import FetchedDocument


class ExplodingString(str):
    def strip(self, chars: str | None = None) -> str:
        raise RuntimeError("parser-secret-must-not-escape")


class BenignString(str):
    pass


class LyingStripString(str):
    def strip(self, chars: str | None = None) -> str:
        return self


def _document(body: bytes, *, content_type: str = "text/html") -> FetchedDocument:
    return FetchedDocument(
        canonical_url="https://example.com/report",
        content_type=content_type,
        body=body,
    )


def test_extracts_title_and_main_text_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    def network_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network downloader must not be called")

    monkeypatch.setattr(trafilatura, "fetch_url", network_forbidden)
    monkeypatch.setattr(trafilatura.downloads, "fetch_url", network_forbidden)
    html = b"""
        <html><head><title>Ignored browser title</title></head><body>
        <nav>Navigation</nav><article><h1>Siemens annual report</h1>
        <p>Siemens reported a meaningful result in this sufficiently long fixture
        paragraph used to exercise local main-content extraction.</p></article>
        </body></html>
    """

    extracted = LocalExtractor().extract(_document(html))

    assert extracted.canonical_url == "https://example.com/report"
    assert extracted.title == "Siemens annual report"
    assert "Siemens reported a meaningful result" in extracted.text
    with pytest.raises(FrozenInstanceError):
        extracted.text = "changed"  # type: ignore[misc]


def test_html_charset_is_detected_from_fetched_bytes() -> None:
    html = """
        <html><head><meta charset="iso-8859-1"><title>Überblick</title></head>
        <body><article><h1>Überblick</h1><p>Siemens veröffentlichte einen
        ausreichend langen Nachhaltigkeitsbericht für die lokale Extraktion.
        </p></article></body></html>
    """.encode("iso-8859-1")

    extracted = LocalExtractor().extract(_document(html))

    assert extracted.title == "Überblick"
    assert "veröffentlichte" in extracted.text


def test_disables_comments_tables_links_and_network_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_bare_extraction(body: bytes, **kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(title="Title", text="Main text")

    monkeypatch.setattr(extract_module, "bare_extraction", fake_bare_extraction)

    LocalExtractor().extract(_document(b"<html><p>Main text</p></html>"))

    assert captured["url"] == "https://example.com/report"
    assert captured["include_comments"] is False
    assert captured["include_tables"] is False
    assert captured["include_links"] is False
    assert captured["include_images"] is False
    assert "download" not in captured


def test_extracts_utf8_plain_text_without_html_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def parser_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("HTML parser must not handle text/plain")

    monkeypatch.setattr(extract_module, "bare_extraction", parser_forbidden)

    extracted = LocalExtractor().extract(
        _document("\ufeffSiemens report\nline two".encode(), content_type="text/plain")
    )

    assert extracted.title is None
    assert extracted.text == "Siemens report\nline two"


def test_plain_text_requires_utf8() -> None:
    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(b"\xff", content_type="text/plain"))

    assert error.value.reason is ExtractionFailureReason.MALFORMED_CONTENT


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"  \n", ExtractionFailureReason.EMPTY_INPUT),
        (b"bad\x00html", ExtractionFailureReason.MALFORMED_CONTENT),
        (b"<html><body></body></html>", ExtractionFailureReason.NO_CONTENT),
    ],
)
def test_empty_malformed_and_contentless_inputs_are_typed(
    body: bytes,
    reason: ExtractionFailureReason,
) -> None:
    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(body))

    assert error.value.reason is reason


def test_input_and_output_limits_are_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ExtractionError) as input_error:
        LocalExtractor(max_input_bytes=3).extract(_document(b"four"))

    monkeypatch.setattr(
        extract_module,
        "bare_extraction",
        lambda *args, **kwargs: SimpleNamespace(title="Title", text="too long"),
    )
    with pytest.raises(ExtractionError) as output_error:
        LocalExtractor(max_output_chars=5).extract(_document(b"<p>text</p>"))

    assert input_error.value.reason is ExtractionFailureReason.INPUT_TOO_LARGE
    assert output_error.value.reason is ExtractionFailureReason.OUTPUT_TOO_LARGE


def test_parser_failure_is_mapped_to_malformed_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_parser(*args: object, **kwargs: object) -> None:
        raise ValueError("parser rejected input")

    monkeypatch.setattr(extract_module, "bare_extraction", fail_parser)

    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(b"<html>broken"))

    assert error.value.reason is ExtractionFailureReason.MALFORMED_CONTENT


@pytest.mark.parametrize(
    "malformed",
    [object(), SimpleNamespace(title="Title", text=object())],
)
def test_malformed_parser_result_is_typed(
    monkeypatch: pytest.MonkeyPatch, malformed: object
) -> None:
    monkeypatch.setattr(
        extract_module, "bare_extraction", lambda *args, **kwargs: malformed
    )

    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(b"<html>content</html>"))

    assert error.value.reason is ExtractionFailureReason.MALFORMED_CONTENT


@pytest.mark.parametrize("field", ["text", "title"])
def test_hostile_string_fields_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    result = SimpleNamespace(title="Title", text="Main text")
    setattr(result, field, ExplodingString("hostile"))
    monkeypatch.setattr(
        extract_module, "bare_extraction", lambda *args, **kwargs: result
    )

    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(b"<html>content</html>"))

    assert error.value.reason is ExtractionFailureReason.MALFORMED_CONTENT
    assert error.value.__cause__ is None
    assert "parser-secret" not in str(error.value)


def test_extractor_returns_plain_strings_from_subclass_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extract_module,
        "bare_extraction",
        lambda *args, **kwargs: SimpleNamespace(
            title=BenignString(" Title "), text=BenignString(" Main text ")
        ),
    )

    extracted = LocalExtractor().extract(_document(b"<html>content</html>"))

    assert type(extracted.title) is str
    assert type(extracted.text) is str
    assert extracted.title == "Title"
    assert extracted.text == "Main text"


def test_string_subclass_cannot_forge_nonempty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extract_module,
        "bare_extraction",
        lambda *args, **kwargs: SimpleNamespace(
            title=None, text=LyingStripString("   ")
        ),
    )

    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(b"<html>content</html>"))

    assert error.value.reason is ExtractionFailureReason.NO_CONTENT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_input_bytes": 0},
        {"max_input_bytes": True},
        {"max_output_chars": 0},
    ],
)
def test_extractor_rejects_invalid_limits(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        LocalExtractor(**kwargs)  # type: ignore[arg-type]
