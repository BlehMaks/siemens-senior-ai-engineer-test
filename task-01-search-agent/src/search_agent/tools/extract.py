"""Local-only main-content extraction from already fetched bytes."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from html.parser import HTMLParser
from io import BytesIO
from math import isfinite
from pathlib import Path

from pypdf import PdfReader
from trafilatura import bare_extraction

from .fetch import FetchedDocument, _validated_fetched_document


def _worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = package_root
    return environment


class ExtractionFailureReason(StrEnum):
    EMPTY_INPUT = "empty_input"
    INPUT_TOO_LARGE = "input_too_large"
    ENCRYPTED_DOCUMENT = "encrypted_document"
    MALFORMED_CONTENT = "malformed_content"
    NO_CONTENT = "no_content"
    OUTPUT_TOO_LARGE = "output_too_large"
    PAGE_LIMIT = "page_limit"
    CONTENT_STREAM_TOO_LARGE = "content_stream_too_large"
    TIMEOUT = "timeout"


class ExtractionError(RuntimeError):
    def __init__(self, reason: ExtractionFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ExtractedBlock:
    text: str
    page_number: int | None = None
    section: str | None = None
    table_index: int | None = None


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    canonical_url: str
    title: str | None
    text: str
    media_type: str = "text/html"
    blocks: tuple[ExtractedBlock, ...] = ()
    published_at: datetime | None = None
    updated_at: datetime | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class LocalExtractor:
    max_input_bytes: int = 2 * 1024 * 1024
    max_output_chars: int = 100_000
    max_pdf_pages: int = 64
    max_pdf_content_stream_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in (
            ("max_input_bytes", self.max_input_bytes),
            ("max_output_chars", self.max_output_chars),
            ("max_pdf_pages", self.max_pdf_pages),
            ("max_pdf_content_stream_bytes", self.max_pdf_content_stream_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def extract(self, document: FetchedDocument) -> ExtractedDocument:
        document = _validated_fetched_document(document)
        body = document.body
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
        if document.content_type == "application/pdf":
            return self._extract_pdf(document)
        if b"\x00" in body:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "fetched content is malformed",
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
                include_tables=True,
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
            raw_date = getattr(extracted, "date", None)
            raw_language = getattr(extracted, "language", None)
            if not isinstance(raw_text, (str, type(None))) or not isinstance(
                raw_title, (str, type(None))
            ):
                raise TypeError
            checked_text = raw_text.strip() if isinstance(raw_text, str) else ""
            checked_title = raw_title.strip() if isinstance(raw_title, str) else ""
            if not isinstance(checked_text, str) or not isinstance(checked_title, str):
                raise TypeError
            # Exercise parser hooks only to detect failures. Base operations on the
            # original value prevent a subclass from changing whitespace semantics.
            text = (
                str.strip(str.__getitem__(raw_text, slice(None)))
                if isinstance(raw_text, str)
                else ""
            )
            title = (
                str.strip(str.__getitem__(raw_title, slice(None))) or None
                if isinstance(raw_title, str)
                else None
            )
            published_at = _html_publication_time(raw_date)
            language = _language(raw_language)
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
        if len(text) + (len(title) if title is not None else 0) > self.max_output_chars:
            raise ExtractionError(
                ExtractionFailureReason.OUTPUT_TOO_LARGE,
                "extracted content exceeds the output limit",
            )
        visible_blocks = _extract_html_visible_blocks(body)
        table_blocks = tuple(
            ExtractedBlock(text=table_text, table_index=index)
            for index, table_text in enumerate(_extract_html_tables(body), start=1)
        )
        candidate_blocks = _deduplicated_blocks((*visible_blocks, *table_blocks))
        text = _append_missing_blocks(
            text,
            candidate_blocks,
            max_chars=self.max_output_chars - (len(title) if title is not None else 0),
        )
        blocks = _html_blocks(candidate_blocks, source_text=text)
        return ExtractedDocument(
            canonical_url=document.canonical_url,
            title=title,
            text=text,
            media_type=document.content_type,
            blocks=blocks or (ExtractedBlock(text=text),),
            published_at=published_at,
            language=language,
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
            media_type=document.content_type,
            blocks=(ExtractedBlock(text=text),),
        )

    def _extract_pdf(self, document: FetchedDocument) -> ExtractedDocument:
        try:
            reader = PdfReader(BytesIO(document.body), strict=True)
            if reader.is_encrypted:
                raise ExtractionError(
                    ExtractionFailureReason.ENCRYPTED_DOCUMENT,
                    "encrypted PDF documents are not supported",
                )
            page_count = len(reader.pages)
            if page_count > self.max_pdf_pages:
                raise ExtractionError(
                    ExtractionFailureReason.PAGE_LIMIT,
                    "PDF document exceeds the page limit",
                )
            title = _pdf_title(reader)
            published_at, updated_at = _pdf_times(reader)
            pages: list[str] = []
            blocks: list[ExtractedBlock] = []
            content_stream_bytes = 0
            output_chars = len(title) if title is not None else 0
            for page_number, page in enumerate(reader.pages, start=1):
                contents = page.get_contents()
                if contents is not None:
                    content_stream_bytes += len(contents.get_data())
                    if content_stream_bytes > self.max_pdf_content_stream_bytes:
                        raise ExtractionError(
                            ExtractionFailureReason.CONTENT_STREAM_TOO_LARGE,
                            "PDF content streams exceed the extraction limit",
                        )
                raw_text = page.extract_text(extraction_mode="layout")
                if not isinstance(raw_text, (str, type(None))):
                    raise TypeError
                page_text = _clean_extracted_text(raw_text or "")
                if not page_text:
                    continue
                output_chars += len(page_text)
                if pages:
                    output_chars += 2
                if output_chars > self.max_output_chars:
                    raise ExtractionError(
                        ExtractionFailureReason.OUTPUT_TOO_LARGE,
                        "extracted content exceeds the output limit",
                    )
                pages.append(page_text)
                blocks.extend(_pdf_page_blocks(page_text, page_number=page_number))
        except ExtractionError:
            raise
        except Exception:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "PDF extraction failed",
            ) from None
        if not pages:
            raise ExtractionError(
                ExtractionFailureReason.NO_CONTENT,
                "no main content was extracted",
            )
        return ExtractedDocument(
            canonical_url=document.canonical_url,
            title=title,
            text="\n\n".join(pages),
            media_type=document.content_type,
            blocks=tuple(blocks),
            published_at=published_at,
            updated_at=updated_at,
        )


class _HTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[str] = []
        self._suppressed_depth = 0
        self._table_depth = 0
        self._rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._suppressed_depth:
            if tag not in _VOID_HTML_TAGS:
                self._suppressed_depth += 1
            return
        if _starts_suppressed_html(tag, attrs):
            if tag not in _VOID_HTML_TAGS:
                self._suppressed_depth = 1
            return
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._rows = []
            return
        if self._table_depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._table_depth == 1 and self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._suppressed_depth:
            if tag not in _VOID_HTML_TAGS:
                self._suppressed_depth -= 1
            return
        if self._table_depth == 1 and tag in {"td", "th"}:
            if self._row is not None and self._cell_parts is not None:
                cell = _space_normalized(" ".join(self._cell_parts))
                self._row.append(cell)
            self._cell_parts = None
        elif self._table_depth == 1 and tag == "tr":
            if self._row is not None and any(self._row):
                self._rows.append(self._row)
            self._row = None
            self._cell_parts = None
        elif tag == "table" and self._table_depth:
            if self._table_depth == 1:
                rendered = "\n".join(
                    " | ".join(cell for cell in row if cell) for row in self._rows
                ).strip()
                if rendered:
                    self.tables.append(rendered)
                self._rows = []
                self._row = None
                self._cell_parts = None
            self._table_depth -= 1


class _HTMLBlockParser(HTMLParser):
    _BLOCK_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "pre"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[ExtractedBlock] = []
        self._suppressed_depth = 0
        self._active_tag: str | None = None
        self._parts: list[str] = []
        self._section: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._suppressed_depth:
            if tag not in _VOID_HTML_TAGS:
                self._suppressed_depth += 1
            return
        if _starts_suppressed_html(tag, attrs):
            if tag not in _VOID_HTML_TAGS:
                self._suppressed_depth = 1
            return
        if tag not in self._BLOCK_TAGS:
            return
        if self._active_tag is not None:
            self._finish_block()
        self._active_tag = tag
        self._parts = []

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth and self._active_tag is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._suppressed_depth:
            if tag not in _VOID_HTML_TAGS:
                self._suppressed_depth -= 1
            return
        if tag == self._active_tag:
            self._finish_block()

    def close(self) -> None:
        super().close()
        self._finish_block()

    def _finish_block(self) -> None:
        if self._active_tag is None:
            return
        text = _space_normalized(" ".join(self._parts))
        if text:
            if self._active_tag.startswith("h"):
                self._section = text
            self.blocks.append(ExtractedBlock(text=text, section=self._section))
        self._active_tag = None
        self._parts = []


_VOID_HTML_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_NON_VISIBLE_HTML_TAGS = frozenset({"script", "style", "template", "noscript"})


def _starts_suppressed_html(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag in _NON_VISIBLE_HTML_TAGS:
        return True
    for raw_name, raw_value in attrs:
        name = raw_name.casefold()
        if name in {"hidden", "inert"}:
            return True
        value = (raw_value or "").strip().casefold()
        if name == "aria-hidden" and value == "true":
            return True
        if name != "style":
            continue
        value = re.sub(r"/\*.*?(?:\*/|$)", "", value, flags=re.DOTALL)
        for declaration in value.split(";"):
            property_name, separator, property_value = declaration.partition(":")
            checked_value = property_value.split("!", 1)[0].strip()
            if separator and (
                (property_name.strip() == "display" and checked_value == "none")
                or (property_name.strip() == "visibility" and checked_value == "hidden")
            ):
                return True
    return False


def _extract_html_tables(body: bytes) -> tuple[str, ...]:
    try:
        rendered = _decode_html(body)
        parser = _HTMLTableParser()
        parser.feed(rendered)
        parser.close()
        return tuple(parser.tables)
    except Exception:
        return ()


def _extract_html_visible_blocks(body: bytes) -> tuple[ExtractedBlock, ...]:
    try:
        rendered = _decode_html(body)
        parser = _HTMLBlockParser()
        parser.feed(rendered)
        parser.close()
        return _deduplicated_blocks(tuple(parser.blocks))
    except Exception:
        return ()


def _append_missing_blocks(
    source_text: str,
    blocks: tuple[ExtractedBlock, ...],
    *,
    max_chars: int,
) -> str:
    normalized_source = _space_normalized(source_text)
    for block in blocks:
        normalized_block = _space_normalized(block.text)
        if not normalized_block or normalized_block in normalized_source:
            continue
        addition = f"\n\n{block.text}"
        if len(source_text) + len(addition) > max_chars:
            continue
        source_text += addition
        normalized_source = _space_normalized(source_text)
    return source_text


def _html_blocks(
    candidates: tuple[ExtractedBlock, ...], *, source_text: str
) -> tuple[ExtractedBlock, ...]:
    try:
        normalized_source = _space_normalized(source_text)
        selected = [
            block
            for block in candidates
            if _space_normalized(block.text) in normalized_source
        ]
        return _deduplicated_blocks(tuple(selected))
    except Exception:
        return ()


def _decode_html(body: bytes) -> str:
    charset_match = re.search(
        rb"charset\s*=\s*['\"]?([a-zA-Z0-9._-]{1,40})",
        body[:4096],
        flags=re.IGNORECASE,
    )
    charset = charset_match.group(1).decode("ascii") if charset_match else "utf-8"
    return body.decode(charset, errors="replace")


def _deduplicated_blocks(
    values: tuple[ExtractedBlock, ...],
) -> tuple[ExtractedBlock, ...]:
    seen: set[tuple[str, int | None, str | None, int | None]] = set()
    result: list[ExtractedBlock] = []
    for block in values:
        key = (
            _space_normalized(block.text),
            block.page_number,
            block.section,
            block.table_index,
        )
        if key not in seen:
            seen.add(key)
            result.append(block)
    return tuple(result)


def _space_normalized(value: str) -> str:
    return " ".join(value.split())


def _pdf_title(reader: PdfReader) -> str | None:
    metadata = reader.metadata
    if metadata is None or metadata.title is None:
        return None
    raw_title = metadata.title
    if not isinstance(raw_title, str):
        raise TypeError
    return str.strip(str.__getitem__(raw_title, slice(None))) or None


def _pdf_times(reader: PdfReader) -> tuple[datetime | None, datetime | None]:
    metadata = reader.metadata
    if metadata is None:
        return None, None
    return (
        _utc_time(metadata.creation_date),
        _utc_time(metadata.modification_date),
    )


def _html_publication_time(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError
    rendered = str.strip(str.__getitem__(value, slice(None)))
    if not rendered:
        return None
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(rendered, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None
    return _utc_time(parsed)


def _utc_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _language(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError
    rendered = str.strip(str.__getitem__(value, slice(None))).lower()
    if not rendered:
        return None
    if (
        len(rendered) > 35
        or re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]{1,8})*", rendered) is None
    ):
        return None
    return rendered


def _clean_extracted_text(value: str) -> str:
    plain = str.__getitem__(value, slice(None)).replace("\x00", "")
    lines = [line.rstrip() for line in plain.replace("\r\n", "\n").split("\n")]
    cleaned: list[str] = []
    for line in lines:
        if line or (cleaned and cleaned[-1]):
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def _pdf_page_blocks(text: str, *, page_number: int) -> tuple[ExtractedBlock, ...]:
    lines = text.splitlines()
    blocks: list[ExtractedBlock] = []
    buffer: list[str] = []
    buffer_is_table: bool | None = None
    section: str | None = None
    table_index = 0

    def flush() -> None:
        nonlocal buffer, table_index
        rendered = "\n".join(buffer).strip()
        if rendered:
            current_table = buffer_is_table is True
            if current_table:
                table_index += 1
            blocks.append(
                ExtractedBlock(
                    text=rendered,
                    page_number=page_number,
                    section=section,
                    table_index=table_index if current_table else None,
                )
            )
        buffer = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            buffer_is_table = None
            continue
        is_table = _looks_like_table_line(line)
        if buffer and is_table != buffer_is_table:
            flush()
        if not is_table and _looks_like_heading(stripped):
            flush()
            section = stripped
        buffer_is_table = is_table
        buffer.append(line)
    flush()
    return tuple(blocks) or (ExtractedBlock(text=text, page_number=page_number),)


def _looks_like_table_line(line: str) -> bool:
    return "\t" in line or re.search(r"\S\s{2,}\S", line) is not None


def _looks_like_heading(line: str) -> bool:
    words = line.split()
    return (
        1 <= len(words) <= 12
        and len(line) <= 120
        and not line.endswith((".", ":", ";", ","))
        and (line.isupper() or line.istitle())
    )


@dataclass(frozen=True, slots=True)
class AsyncLocalExtractor:
    """Run bounded local parsing in a process that cancellation can terminate."""

    extractor: LocalExtractor = field(default_factory=LocalExtractor)
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not isfinite(float(self.timeout_seconds))
            or not 0.1 <= float(self.timeout_seconds) <= 60.0
        ):
            raise ValueError("extraction timeout must be between 0.1 and 60 seconds")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

    async def extract(self, document: FetchedDocument) -> ExtractedDocument:
        try:
            document = _validated_fetched_document(document)
        except TypeError:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "fetched content is malformed",
            ) from None
        if len(document.body) > self.extractor.max_input_bytes:
            raise ExtractionError(
                ExtractionFailureReason.INPUT_TOO_LARGE,
                "fetched content exceeds the extraction input limit",
            )
        payload = json.dumps(
            {
                "body": base64.b64encode(document.body).decode("ascii"),
                "canonical_url": document.canonical_url,
                "content_type": document.content_type,
                "max_input_bytes": self.extractor.max_input_bytes,
                "max_output_chars": self.extractor.max_output_chars,
                "max_pdf_pages": self.extractor.max_pdf_pages,
                "max_pdf_content_stream_bytes": (
                    self.extractor.max_pdf_content_stream_bytes
                ),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "search_agent.tools.extract",
            "--worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_worker_environment(),
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(payload),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            await _terminate_worker(process)
            raise ExtractionError(
                ExtractionFailureReason.TIMEOUT,
                "content extraction timed out",
            ) from None
        except BaseException:
            await _terminate_worker(process)
            raise
        if process.returncode != 0:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "content extraction process failed",
            )
        return _decode_worker_result(
            stdout,
            expected_url=document.canonical_url,
            expected_media_type=document.content_type,
            max_output_chars=self.extractor.max_output_chars,
            max_blocks=self.extractor.max_pdf_pages * 64,
        )


async def _terminate_worker(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.kill()
    reap_task = asyncio.create_task(process.wait())
    while not reap_task.done():
        try:
            await asyncio.shield(reap_task)
        except asyncio.CancelledError:
            continue
    with suppress(Exception):
        reap_task.result()


def _decode_worker_result(
    payload: bytes,
    *,
    expected_url: str,
    expected_media_type: str,
    max_output_chars: int,
    max_blocks: int,
) -> ExtractedDocument:
    try:
        if not isinstance(payload, bytes):
            raise ValueError
        rendered = bytes.__getitem__(payload, slice(None))
        if type(rendered) is not bytes:
            raise ValueError
        if bytes.__len__(rendered) > max_output_chars * 12 + 4096:
            raise ExtractionError(
                ExtractionFailureReason.OUTPUT_TOO_LARGE,
                "content extraction process output exceeds the limit",
            )
        result = json.loads(rendered)
        if not isinstance(result, dict) or set(result) not in (
            {"ok", "reason"},
            {"ok", "canonical_url", "title", "text"},
            {
                "ok",
                "canonical_url",
                "title",
                "text",
                "media_type",
                "blocks",
            },
            {
                "ok",
                "canonical_url",
                "title",
                "text",
                "media_type",
                "blocks",
                "published_at",
                "updated_at",
                "language",
            },
        ):
            raise ValueError
        if result["ok"] is not True:
            reason = ExtractionFailureReason(result["reason"])
            raise ExtractionError(reason, "content extraction failed")
        canonical_url = result["canonical_url"]
        title = result["title"]
        text = result["text"]
        media_type = result.get("media_type", expected_media_type)
        raw_blocks = result.get("blocks", [])
        published_at = _decode_time(result.get("published_at"))
        updated_at = _decode_time(result.get("updated_at"))
        language = _language(result.get("language"))
        if (
            type(canonical_url) is not str
            or (title is not None and type(title) is not str)
            or type(text) is not str
            or type(media_type) is not str
            or type(raw_blocks) is not list
            or canonical_url != expected_url
            or media_type != expected_media_type
        ):
            raise ValueError
        text = text.strip()
        if title is not None:
            title = title.strip() or None
        if not text:
            raise ExtractionError(
                ExtractionFailureReason.NO_CONTENT,
                "content extraction process returned no content",
            )
        if len(text) + (len(title) if title is not None else 0) > max_output_chars:
            raise ExtractionError(
                ExtractionFailureReason.OUTPUT_TOO_LARGE,
                "content extraction process output exceeds the limit",
            )
        blocks = _decode_blocks(
            raw_blocks,
            text=text,
            max_output_chars=max_output_chars,
            max_blocks=max_blocks,
        )
        return ExtractedDocument(
            canonical_url=canonical_url,
            title=title,
            text=text,
            media_type=media_type,
            blocks=blocks,
            published_at=published_at,
            updated_at=updated_at,
            language=language,
        )
    except ExtractionError:
        raise
    except Exception:
        raise ExtractionError(
            ExtractionFailureReason.MALFORMED_CONTENT,
            "content extraction process returned an invalid result",
        ) from None


def _decode_blocks(
    values: list[object], *, text: str, max_output_chars: int, max_blocks: int
) -> tuple[ExtractedBlock, ...]:
    if len(values) > max_blocks:
        raise ValueError
    blocks: list[ExtractedBlock] = []
    block_chars = 0
    normalized_text = _space_normalized(text)
    for value in values:
        if type(value) is not dict or set(value) != {
            "text",
            "page_number",
            "section",
            "table_index",
        }:
            raise ValueError
        block_text = value["text"]
        page_number = value["page_number"]
        section = value["section"]
        table_index = value["table_index"]
        if (
            type(block_text) is not str
            or not block_text.strip()
            or (page_number is not None and type(page_number) is not int)
            or (section is not None and type(section) is not str)
            or (table_index is not None and type(table_index) is not int)
            or (page_number is not None and page_number < 1)
            or (table_index is not None and table_index < 1)
            or (section is not None and not section.strip())
        ):
            raise ValueError
        block_text = block_text.strip()
        section = section.strip() if section is not None else None
        block_chars += len(block_text)
        if (
            block_chars > max_output_chars
            or _space_normalized(block_text) not in normalized_text
        ):
            raise ValueError
        blocks.append(
            ExtractedBlock(
                text=block_text,
                page_number=page_number,
                section=section,
                table_index=table_index,
            )
        )
    return tuple(blocks)


def _decode_time(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _worker_main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read())
        if not isinstance(payload, dict) or set(payload) != {
            "body",
            "canonical_url",
            "content_type",
            "max_input_bytes",
            "max_output_chars",
            "max_pdf_pages",
            "max_pdf_content_stream_bytes",
        }:
            raise ValueError
        document = FetchedDocument(
            canonical_url=payload["canonical_url"],
            content_type=payload["content_type"],
            body=base64.b64decode(payload["body"], validate=True),
        )
        extracted = LocalExtractor(
            max_input_bytes=payload["max_input_bytes"],
            max_output_chars=payload["max_output_chars"],
            max_pdf_pages=payload["max_pdf_pages"],
            max_pdf_content_stream_bytes=payload["max_pdf_content_stream_bytes"],
        ).extract(document)
        result = {
            "ok": True,
            "canonical_url": extracted.canonical_url,
            "title": extracted.title,
            "text": extracted.text,
            "media_type": extracted.media_type,
            "blocks": [
                {
                    "text": block.text,
                    "page_number": block.page_number,
                    "section": block.section,
                    "table_index": block.table_index,
                }
                for block in extracted.blocks
            ],
            "published_at": (
                extracted.published_at.isoformat()
                if extracted.published_at is not None
                else None
            ),
            "updated_at": (
                extracted.updated_at.isoformat()
                if extracted.updated_at is not None
                else None
            ),
            "language": extracted.language,
        }
    except ExtractionError as exc:
        result = {"ok": False, "reason": exc.reason.value}
    except Exception:
        result = {
            "ok": False,
            "reason": ExtractionFailureReason.MALFORMED_CONTENT.value,
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    raise SystemExit(_worker_main())
