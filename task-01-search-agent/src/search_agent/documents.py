"""Immutable normalized document contracts for deterministic retrieval."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from .contracts import SearchHit
from .tools import ExtractedBlock, ExtractedDocument

_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
_MAX_DOCUMENT_CHARS = 100_000
_MAX_CHUNK_CHARS = 1_200
_MAX_TITLE_CHARS = 400
_MAX_SECTION_CHARS = 250
_MAX_LANGUAGE_CHARS = 35
_ID_PATTERN = re.compile(r"^(?:doc|chunk)-[a-f0-9]{24}$")


class SourceType(StrEnum):
    OFFICIAL_REPORT = "official_report"
    OFFICIAL_DOCUMENTATION = "official_documentation"
    REGULATORY_FILING = "regulatory_filing"
    GOVERNMENT = "government"
    ACADEMIC = "academic"
    MAJOR_MEDIA = "major_media"
    TECHNICAL_ARTICLE = "technical_article"
    FORUM = "forum"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ResearchDocument:
    document_id: str
    canonical_url: str
    title: str
    domain: str
    media_type: str
    source_type: SourceType
    content_hash: str
    published_at: datetime | None
    updated_at: datetime | None
    retrieved_at: datetime | None
    language: str | None
    text: str
    blocks: tuple[ExtractedBlock, ...]

    def __post_init__(self) -> None:
        if _ID_PATTERN.fullmatch(self.document_id) is None:
            raise ValueError("document_id is invalid")
        if not self.canonical_url or not self.domain:
            raise ValueError("document provenance is required")
        if not 1 <= len(self.title) <= _MAX_TITLE_CHARS:
            raise ValueError("document title is outside its bounds")
        if not 1 <= len(self.text) <= _MAX_DOCUMENT_CHARS:
            raise ValueError("document text is outside its bounds")
        if hashlib.sha256(self.text.encode()).hexdigest() != self.content_hash:
            raise ValueError("document content hash does not match")
        _validate_times(self.published_at, self.updated_at, self.retrieved_at)
        if (
            self.language is not None
            and not 1 <= len(self.language) <= _MAX_LANGUAGE_CHARS
        ):
            raise ValueError("document language is outside its bounds")
        if not self.blocks:
            raise ValueError("document blocks are required")


@dataclass(frozen=True, slots=True)
class ResearchChunk:
    chunk_id: str
    document_id: str
    canonical_url: str
    title: str
    source_type: SourceType
    content_hash: str
    ordinal: int
    page_number: int | None
    section: str | None
    table_index: int | None
    published_at: datetime | None
    updated_at: datetime | None
    retrieved_at: datetime | None
    text: str
    lexical_score: float
    exact_term_score: float
    authority_score: float
    freshness_score: float
    final_score: float

    def __post_init__(self) -> None:
        if _ID_PATTERN.fullmatch(self.chunk_id) is None:
            raise ValueError("chunk_id is invalid")
        if _ID_PATTERN.fullmatch(self.document_id) is None:
            raise ValueError("chunk document_id is invalid")
        if not 1 <= len(self.text) <= _MAX_CHUNK_CHARS:
            raise ValueError("chunk text is outside its bounds")
        if hashlib.sha256(self.text.encode()).hexdigest() != self.content_hash:
            raise ValueError("chunk content hash does not match")
        if self.ordinal < 1:
            raise ValueError("chunk ordinal must be positive")
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("chunk page number must be positive")
        if self.table_index is not None and self.table_index < 1:
            raise ValueError("chunk table index must be positive")
        if (
            self.section is not None
            and not 1 <= len(self.section) <= _MAX_SECTION_CHARS
        ):
            raise ValueError("chunk section is outside its bounds")
        for value in (
            self.lexical_score,
            self.exact_term_score,
            self.authority_score,
            self.freshness_score,
            self.final_score,
        ):
            if type(value) is not float or not 0.0 <= value <= 10.0:
                raise ValueError("chunk score is outside its bounds")
        _validate_times(self.published_at, self.updated_at, self.retrieved_at)


def build_research_document(
    hit: SearchHit,
    extracted: ExtractedDocument,
    *,
    retrieved_at: datetime | None = None,
) -> ResearchDocument:
    """Normalize an extracted source without trusting parser metadata."""

    if type(extracted) is not ExtractedDocument:
        raise ValueError("extracted document has an invalid type")
    canonical_url = _canonical_url(extracted.canonical_url)
    if canonical_url != _canonical_url(str(hit.url)):
        raise ValueError("search and extracted document URLs do not match")
    text = normalize_document_text(extracted.text)
    title = normalize_metadata_text(extracted.title or str(hit.title), _MAX_TITLE_CHARS)
    domain = urlsplit(canonical_url).hostname or ""
    media_type = _media_type(extracted.media_type)
    source_type = classify_source(canonical_url, title=title, media_type=media_type)
    published_at = _utc_time(extracted.published_at) or _snippet_time(
        str(hit.snippet), label="published"
    )
    updated_at = _utc_time(extracted.updated_at) or _snippet_time(
        str(hit.snippet), label="updated"
    )
    checked_retrieved_at = _utc_time(retrieved_at)
    language = _language(extracted.language)
    blocks = _normalized_blocks(extracted.blocks, source_text=text)
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    digest = hashlib.sha256(f"{canonical_url}\n{content_hash}".encode()).hexdigest()
    return ResearchDocument(
        document_id=f"doc-{digest[:24]}",
        canonical_url=canonical_url,
        title=title,
        domain=domain,
        media_type=media_type,
        source_type=source_type,
        content_hash=content_hash,
        published_at=published_at,
        updated_at=updated_at,
        retrieved_at=checked_retrieved_at,
        language=language,
        text=text,
        blocks=blocks,
    )


def normalize_document_text(value: object) -> str:
    if type(value) is not str:
        raise ValueError("document text must be plain text")
    rendered = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(character).startswith("C")
        and character not in {"\n", "\r", "\t"}
        for character in rendered
    ):
        raise ValueError("document text contains unsupported controls")
    lines = [
        " ".join(line.split()) for line in rendered.replace("\r\n", "\n").split("\n")
    ]
    paragraphs: list[str] = []
    for line in lines:
        if line:
            paragraphs.append(line)
        elif paragraphs and paragraphs[-1]:
            paragraphs.append("")
    normalized = "\n".join(paragraphs).strip()
    if not normalized or len(normalized) > _MAX_DOCUMENT_CHARS:
        raise ValueError("document text is empty or exceeds its bound")
    return normalized


def normalize_metadata_text(value: object, limit: int) -> str:
    if type(value) is not str:
        raise ValueError("metadata must be plain text")
    rendered = " ".join(unicodedata.normalize("NFKC", value).split())
    if not rendered or len(rendered) > limit:
        raise ValueError("metadata is empty or exceeds its bound")
    return rendered


def classify_source(url: str, *, title: str, media_type: str) -> SourceType:
    host = (urlsplit(url).hostname or "").lower()
    lowered = title.casefold()
    if host == "siemens.com" or host.endswith(".siemens.com"):
        if media_type == "application/pdf" or any(
            term in lowered
            for term in ("annual report", "sustainability report", "report")
        ):
            return SourceType.OFFICIAL_REPORT
        return SourceType.OFFICIAL_DOCUMENTATION
    if host == "sec.gov" or host.endswith(".sec.gov"):
        return SourceType.REGULATORY_FILING
    if host.endswith(".gov") or host.endswith(".gov.uk"):
        return SourceType.GOVERNMENT
    if host.endswith(".edu") or host in {"arxiv.org", "doi.org"}:
        return SourceType.ACADEMIC
    if host in {"bbc.com", "ft.com", "reuters.com", "wsj.com"}:
        return SourceType.MAJOR_MEDIA
    if host in {"github.com", "stackoverflow.com"}:
        return SourceType.TECHNICAL_ARTICLE
    if host in {"news.ycombinator.com", "reddit.com"} or host.endswith(".reddit.com"):
        return SourceType.FORUM
    return SourceType.UNKNOWN


def _normalized_blocks(
    values: tuple[ExtractedBlock, ...], *, source_text: str
) -> tuple[ExtractedBlock, ...]:
    source_flat = " ".join(source_text.split())
    result: list[ExtractedBlock] = []
    seen: set[tuple[str, int | None, str | None, int | None]] = set()
    for value in values:
        if type(value) is not ExtractedBlock:
            raise ValueError("document block has an invalid type")
        try:
            text = normalize_document_text(value.text)
            section = (
                normalize_metadata_text(value.section, _MAX_SECTION_CHARS)
                if value.section is not None
                else None
            )
        except ValueError:
            continue
        if " ".join(text.split()) not in source_flat:
            continue
        if value.page_number is not None and (
            type(value.page_number) is not int or value.page_number < 1
        ):
            raise ValueError("document block page number is invalid")
        if value.table_index is not None and (
            type(value.table_index) is not int or value.table_index < 1
        ):
            raise ValueError("document block table index is invalid")
        key = (text, value.page_number, section, value.table_index)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            ExtractedBlock(
                text=text,
                page_number=value.page_number,
                section=section,
                table_index=value.table_index,
            )
        )
    return tuple(result) or (ExtractedBlock(text=source_text),)


def _canonical_url(value: object) -> str:
    if type(value) is not str:
        raise ValueError("document URL must be plain text")
    try:
        parsed = _URL_ADAPTER.validate_python(value.strip())
    except ValidationError:
        raise ValueError("document URL is malformed") from None
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("document URL must not contain credentials")
    split = urlsplit(str(parsed))
    return urlunsplit((split.scheme, split.netloc, split.path, split.query, ""))


def _media_type(value: object) -> str:
    if type(value) is not str:
        raise ValueError("document media type must be plain text")
    rendered = value.strip().lower()
    if rendered not in {
        "application/pdf",
        "application/xhtml+xml",
        "text/html",
        "text/plain",
    }:
        raise ValueError("document media type is unsupported")
    return rendered


def _language(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("document language must be plain text")
    rendered = value.strip().lower()
    if (
        not rendered
        or len(rendered) > _MAX_LANGUAGE_CHARS
        or re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]{1,8})*", rendered) is None
    ):
        raise ValueError("document language is malformed")
    return rendered


def _utc_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("document timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _snippet_time(value: str, *, label: str) -> datetime | None:
    match = re.search(
        rf"\b{label}\s*[:=-]?\s*(20\d{{2}}-[01]\d-[0-3]\d)\b",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _validate_times(*values: datetime | None) -> None:
    for value in values:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("document timestamps must be UTC")


__all__ = [
    "ResearchChunk",
    "ResearchDocument",
    "SourceType",
    "build_research_document",
    "classify_source",
    "normalize_document_text",
]
