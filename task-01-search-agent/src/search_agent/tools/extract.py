"""Local-only main-content extraction from already fetched bytes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trafilatura import bare_extraction

from .fetch import FetchedDocument


class ExtractionFailureReason(StrEnum):
    EMPTY_INPUT = "empty_input"
    INPUT_TOO_LARGE = "input_too_large"
    MALFORMED_CONTENT = "malformed_content"
    NO_CONTENT = "no_content"
    OUTPUT_TOO_LARGE = "output_too_large"


class ExtractionError(RuntimeError):
    def __init__(self, reason: ExtractionFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    canonical_url: str
    title: str | None
    text: str


@dataclass(frozen=True, slots=True)
class LocalExtractor:
    max_input_bytes: int = 2 * 1024 * 1024
    max_output_chars: int = 100_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_input_bytes", self.max_input_bytes),
            ("max_output_chars", self.max_output_chars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def extract(self, document: FetchedDocument) -> ExtractedDocument:
        body = document.body
        if not isinstance(body, bytes) or b"\x00" in body:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "fetched content is malformed",
            )
        if not body.strip():
            raise ExtractionError(
                ExtractionFailureReason.EMPTY_INPUT,
                "fetched content is empty",
            )
        if len(body) > self.max_input_bytes:
            raise ExtractionError(
                ExtractionFailureReason.INPUT_TOO_LARGE,
                "fetched content exceeds the extraction input limit",
            )
        if document.content_type == "text/plain":
            return self._extract_plain_text(document)
        if document.content_type not in {"application/xhtml+xml", "text/html"}:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "fetched content type is not extractable",
            )

        try:
            # bare_extraction parses supplied bytes only; no downloader is reachable.
            extracted = bare_extraction(
                body,
                url=document.canonical_url,
                output_format="python",
                include_comments=False,
                include_tables=False,
                include_links=False,
                include_images=False,
                with_metadata=True,
            )
        except Exception:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "content extraction failed",
            ) from None

        if extracted is None:
            raise ExtractionError(
                ExtractionFailureReason.NO_CONTENT,
                "no main content was extracted",
            )
        if isinstance(extracted, dict):
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "content extractor returned an invalid result",
            )
        try:
            raw_text = extracted.text
            raw_title = extracted.title
            if not isinstance(raw_text, (str, type(None))) or not isinstance(
                raw_title, (str, type(None))
            ):
                raise TypeError
            stripped_text = raw_text.strip() if isinstance(raw_text, str) else ""
            stripped_title = raw_title.strip() if isinstance(raw_title, str) else ""
            if not isinstance(stripped_text, str) or not isinstance(
                stripped_title, str
            ):
                raise TypeError
            # Base slicing drops third-party str subclasses without invoking hooks.
            text = str.__getitem__(stripped_text, slice(None))
            title = str.__getitem__(stripped_title, slice(None)) or None
            output_chars = len(text) + (len(title) if title is not None else 0)
        except Exception:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "content extractor returned an invalid result",
            ) from None
        if not text:
            raise ExtractionError(
                ExtractionFailureReason.NO_CONTENT,
                "no main content was extracted",
            )
        if output_chars > self.max_output_chars:
            raise ExtractionError(
                ExtractionFailureReason.OUTPUT_TOO_LARGE,
                "extracted content exceeds the output limit",
            )
        return ExtractedDocument(
            canonical_url=document.canonical_url,
            title=title,
            text=text,
        )

    def _extract_plain_text(self, document: FetchedDocument) -> ExtractedDocument:
        try:
            text = document.body.decode("utf-8-sig").strip()
        except UnicodeDecodeError:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "plain text is not valid UTF-8",
            ) from None
        if not text:
            raise ExtractionError(
                ExtractionFailureReason.NO_CONTENT,
                "no main content was extracted",
            )
        if len(text) > self.max_output_chars:
            raise ExtractionError(
                ExtractionFailureReason.OUTPUT_TOO_LARGE,
                "extracted content exceeds the output limit",
            )
        return ExtractedDocument(
            canonical_url=document.canonical_url,
            title=None,
            text=text,
        )
