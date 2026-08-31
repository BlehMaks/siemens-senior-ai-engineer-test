"""Immutable, provenance-checked evidence records."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import islice

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from .contracts import ExtractedEvidence, SearchHit
from .documents import ResearchChunk, ResearchDocument
from .retrieval import (
    RetrievalError,
    RetrievalFailureReason,
    SelectedContext,
    build_research_document,
    chunk_document,
    retrieve_context,
    validate_selected_context,
)
from .tools import ExtractedDocument

_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
_MAX_SOURCE_CHARS = 100_000
_MAX_PUBLIC_TEXT_CHARS = 400
_MAX_QUOTES = 5
_DEFAULT_MAX_AGE = timedelta(days=30)


class EvidenceFailureReason(StrEnum):
    """Stable reasons for refusing untrusted or unverifiable evidence."""

    INVALID_DATA = "invalid_data"
    SOURCE_MISMATCH = "source_mismatch"
    STALE = "stale"
    UNSUPPORTED_QUOTE = "unsupported_quote"


class EvidenceValidationError(RuntimeError):
    def __init__(self, reason: EvidenceFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Public evidence plus the immutable material used to verify it."""

    public: ExtractedEvidence
    retrieved_at: datetime
    source_text: str
    content_hash: str
    source_title: str
    title_provenance_hash: str
    selected_chunks_provenance_hash: str
    selected_context: SelectedContext | None = None
    selected_chunks: tuple[ResearchChunk, ...] = ()

    @property
    def evidence_id(self) -> str:
        return self.public.evidence_id

    @property
    def source_url(self) -> str:
        return str(self.public.source_url)


def build_evidence(
    hit: SearchHit,
    document: ExtractedDocument,
    *,
    retrieved_at: datetime,
    quotes: Sequence[str] = (),
    request_text: str | None = None,
    selected_context: SelectedContext | None = None,
    selected_chunks: Sequence[ResearchChunk] = (),
    now: datetime | None = None,
    max_age: timedelta = _DEFAULT_MAX_AGE,
) -> EvidenceRecord:
    """Build evidence only when search and fetch URL provenance agree exactly."""

    checked_now = _utc_time(now or datetime.now(UTC), field="now")
    checked_retrieved_at = _utc_time(retrieved_at, field="retrieved_at")
    _validate_age(checked_retrieved_at, checked_now, max_age)

    hit_url = _canonical_url(str(hit.url))
    document_url = _canonical_url(document.canonical_url)
    if hit_url != document_url:
        raise EvidenceValidationError(
            EvidenceFailureReason.SOURCE_MISMATCH,
            "search and extracted document URLs do not match",
        )

    search_title = _normalize_text(hit.title, field="search title", limit=400)
    title = (
        search_title
        if document.title is None
        else _normalize_text(document.title, field="document title", limit=400)
    )

    source_text = _normalize_text(
        document.text,
        field="source text",
        limit=_MAX_SOURCE_CHARS,
    )
    checked_chunks = _materialized_chunks(selected_chunks)
    research_document = None
    try:
        if selected_context is not None or checked_chunks:
            research_document = build_research_document(
                hit,
                document,
                retrieved_at=checked_retrieved_at,
            )
        if selected_context is not None:
            validate_selected_context(selected_context)
            if research_document is None:
                raise RetrievalError(
                    reason=RetrievalFailureReason.INVALID_CONTEXT,
                    message="research document was not materialized",
                )
            if any(
                chunk.document_id != research_document.document_id
                for chunk in selected_context.chunks
            ):
                raise RetrievalError(
                    reason=RetrievalFailureReason.SOURCE_MISMATCH,
                    message="selected context belongs to another document",
                )
        elif request_text is not None and not quotes:
            selected_context = retrieve_context(
                request_text,
                hit,
                document,
                retrieved_at=checked_retrieved_at,
            )
        if research_document is not None:
            provenance_chunks = checked_chunks
            if selected_context is not None:
                provenance_chunks = (*provenance_chunks, *selected_context.chunks)
            _validate_chunk_provenance(
                provenance_chunks,
                research_document,
                request_text=request_text or research_document.title,
            )
    except RetrievalError:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "retrieved context is invalid",
        ) from None
    if selected_context is not None:
        quotes = selected_context.quotes
    normalized_quotes = _validated_quotes(quotes, source_text)
    if checked_chunks:
        if research_document is None:
            raise EvidenceValidationError(
                EvidenceFailureReason.INVALID_DATA,
                "selected chunk provenance does not match evidence",
            )
        for chunk in checked_chunks:
            chunk_quote = _normalize_text(
                chunk.text[:_MAX_PUBLIC_TEXT_CHARS].rstrip(),
                field="selected chunk quote",
                limit=_MAX_PUBLIC_TEXT_CHARS,
            )
            if chunk_quote not in normalized_quotes:
                raise EvidenceValidationError(
                    EvidenceFailureReason.INVALID_DATA,
                    "selected chunk provenance does not match evidence",
                )
    content_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    evidence_id = _evidence_id(hit_url, content_hash)
    public = ExtractedEvidence(
        evidence_id=evidence_id,
        source_url=_URL_ADAPTER.validate_python(hit_url),
        source_title=title,
        # The summary is an excerpt, not generated prose, so it remains directly
        # auditable against the normalized source.
        summary=source_text[:_MAX_PUBLIC_TEXT_CHARS].rstrip(),
        quotes=normalized_quotes,
    )
    return EvidenceRecord(
        public=public,
        retrieved_at=checked_retrieved_at,
        source_text=source_text,
        content_hash=content_hash,
        source_title=title,
        title_provenance_hash=_title_provenance_hash(hit_url, title),
        selected_chunks_provenance_hash=_selected_chunks_provenance_hash(
            checked_chunks
        ),
        selected_context=selected_context,
        selected_chunks=checked_chunks,
    )


def validate_record(record: EvidenceRecord) -> None:
    """Re-check record integrity before it crosses the answer boundary."""

    try:
        if (
            type(record) is not EvidenceRecord
            or type(record.public) is not ExtractedEvidence
            or type(record.public.quotes) is not tuple
        ):
            raise ValueError("evidence must use its exact public types")
        public = ExtractedEvidence.model_validate(
            record.public.model_dump(mode="python", warnings="error"),
            strict=True,
        )
        if public != record.public:
            raise ValueError("public evidence is not strictly normalized")
        source_text = _normalize_text(
            record.source_text,
            field="source text",
            limit=_MAX_SOURCE_CHARS,
        )
        if source_text != record.source_text:
            raise ValueError("source text is not normalized")
        source_title = _normalize_text(
            record.source_title,
            field="source title",
            limit=_MAX_PUBLIC_TEXT_CHARS,
        )
        if source_title != record.source_title or public.source_title != source_title:
            raise ValueError("source title does not match provenance")
        content_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        source_url = _canonical_url(str(public.source_url))
        if record.content_hash != content_hash:
            raise ValueError("content hash does not match source text")
        # This builder-derived checksum enforces record consistency. It is not
        # authentication against an attacker who can reconstruct every field.
        if record.title_provenance_hash != _title_provenance_hash(
            source_url, source_title
        ):
            raise ValueError("source title provenance does not match")
        if public.evidence_id != _evidence_id(source_url, content_hash):
            raise ValueError("evidence id does not match source provenance")
        expected_summary = source_text[:_MAX_PUBLIC_TEXT_CHARS].rstrip()
        if public.summary != expected_summary:
            raise ValueError("summary does not match source text")
        if _validated_quotes(public.quotes, source_text) != public.quotes:
            raise ValueError("quotes are not strictly normalized")
        if record.selected_chunks_provenance_hash != (
            _selected_chunks_provenance_hash(record.selected_chunks)
        ):
            raise ValueError("selected chunk provenance does not match")
        if record.selected_context is not None:
            validate_selected_context(record.selected_context)
            if public.quotes != record.selected_context.quotes:
                raise ValueError("quotes do not match selected context")
        _utc_time(record.retrieved_at, field="retrieved_at")
    except Exception:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "evidence record failed its integrity check",
        ) from None


def _validated_quotes(quotes: object, source_text: str) -> tuple[str, ...]:
    if isinstance(quotes, (str, bytes)) or not isinstance(quotes, Sequence):
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "quotes must be a bounded sequence",
        )
    try:
        materialized_quotes = tuple(islice(quotes, _MAX_QUOTES + 1))
    except Exception:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "quotes must be a bounded sequence",
        ) from None
    if len(materialized_quotes) > _MAX_QUOTES:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "quotes must be a bounded sequence",
        )
    normalized: list[str] = []
    for quote in materialized_quotes:
        candidate = _normalize_text(
            quote,
            field="quote",
            limit=_MAX_PUBLIC_TEXT_CHARS,
        )
        if candidate not in source_text:
            raise EvidenceValidationError(
                EvidenceFailureReason.UNSUPPORTED_QUOTE,
                "evidence quote does not occur in source text",
            )
        if candidate not in normalized:
            normalized.append(candidate)
    return tuple(normalized)


def _materialized_chunks(values: object) -> tuple[ResearchChunk, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "selected chunks must be a bounded sequence",
        )
    try:
        chunks = tuple(islice(values, _MAX_QUOTES + 1))
    except Exception:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "selected chunks must be a bounded sequence",
        ) from None
    if len(chunks) > _MAX_QUOTES:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "selected chunks must be a bounded sequence",
        )
    return chunks


def _validate_chunk_provenance(
    chunks: Sequence[ResearchChunk],
    document: ResearchDocument,
    *,
    request_text: str,
) -> None:
    if not chunks:
        return
    if any(type(chunk) is not ResearchChunk for chunk in chunks):
        raise RetrievalError(
            RetrievalFailureReason.INVALID_CONTEXT,
            "selected chunk has an invalid type",
        )
    canonical_by_id = {
        chunk.chunk_id: chunk for chunk in chunk_document(request_text, document)
    }
    for chunk in chunks:
        canonical = canonical_by_id.get(chunk.chunk_id)
        if canonical is None or _chunk_provenance(chunk) != _chunk_provenance(
            canonical
        ):
            raise RetrievalError(
                RetrievalFailureReason.SOURCE_MISMATCH,
                "selected chunk provenance does not match its document",
            )


def _chunk_provenance(chunk: ResearchChunk) -> tuple[object, ...]:
    return (
        chunk.chunk_id,
        chunk.document_id,
        chunk.canonical_url,
        chunk.title,
        chunk.source_type,
        chunk.content_hash,
        chunk.ordinal,
        chunk.page_number,
        chunk.section,
        chunk.table_index,
        chunk.published_at,
        chunk.updated_at,
        chunk.retrieved_at,
        chunk.text,
    )


def _selected_chunks_provenance_hash(chunks: object) -> str:
    if type(chunks) is not tuple or any(
        type(chunk) is not ResearchChunk for chunk in chunks
    ):
        raise ValueError("selected chunks must use exact immutable types")
    payload = "\n".join(repr(_chunk_provenance(chunk)) for chunk in chunks)
    return hashlib.sha256(payload.encode()).hexdigest()


def _normalize_text(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            f"{field} must be text",
        )
    if any(
        unicodedata.category(character).startswith("C") and not character.isspace()
        for character in value
    ):
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            f"{field} contains unsupported control characters",
        )
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or len(normalized) > limit:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            f"{field} is empty or exceeds its size limit",
        )
    return normalized


def _canonical_url(value: str) -> str:
    try:
        parsed = _URL_ADAPTER.validate_python(value.strip())
    except (AttributeError, ValidationError):
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "source URL is malformed",
        ) from None
    if parsed.username is not None or parsed.password is not None:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "source URL must not contain credentials",
        )
    return str(parsed).partition("#")[0]


def _evidence_id(source_url: str, content_hash: str) -> str:
    digest = hashlib.sha256(f"{source_url}\n{content_hash}".encode()).hexdigest()
    return f"ev-{digest[:24]}"


def _title_provenance_hash(source_url: str, source_title: str) -> str:
    return hashlib.sha256(f"{source_url}\n{source_title}".encode()).hexdigest()


def _utc_time(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            f"{field} must be timezone-aware UTC",
        )
    if value.utcoffset() != timedelta(0):
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            f"{field} must be UTC",
        )
    return value.astimezone(UTC)


def _validate_age(retrieved_at: datetime, now: datetime, max_age: timedelta) -> None:
    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "max_age must be positive",
        )
    if retrieved_at > now:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "retrieval time must not be in the future",
        )
    if now - retrieved_at > max_age:
        raise EvidenceValidationError(
            EvidenceFailureReason.STALE,
            "evidence is stale",
        )
