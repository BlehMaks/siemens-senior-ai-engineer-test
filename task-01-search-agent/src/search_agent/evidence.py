"""Immutable, provenance-checked evidence records."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from .contracts import ExtractedEvidence, SearchHit
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
    now: datetime | None = None,
    max_age: timedelta = _DEFAULT_MAX_AGE,
) -> EvidenceRecord:
    """Build evidence only when search and fetch provenance agree exactly."""

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

    title = _normalize_text(hit.title, field="source title", limit=400)
    document_title = _normalize_text(
        document.title,
        field="document title",
        limit=400,
    )
    if document_title != title:
        raise EvidenceValidationError(
            EvidenceFailureReason.SOURCE_MISMATCH,
            "search and extracted document titles do not match",
        )

    source_text = _normalize_text(
        document.text,
        field="source text",
        limit=_MAX_SOURCE_CHARS,
    )
    normalized_quotes = _validated_quotes(quotes, source_text)
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
    )


def validate_record(record: EvidenceRecord) -> None:
    """Re-check record integrity before it crosses the answer boundary."""

    try:
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
        if public.evidence_id != _evidence_id(source_url, content_hash):
            raise ValueError("evidence id does not match source provenance")
        expected_summary = source_text[:_MAX_PUBLIC_TEXT_CHARS].rstrip()
        if public.summary != expected_summary:
            raise ValueError("summary does not match source text")
        if _validated_quotes(public.quotes, source_text) != public.quotes:
            raise ValueError("quotes are not strictly normalized")
        _utc_time(record.retrieved_at, field="retrieved_at")
    except Exception:
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "evidence record failed its integrity check",
        ) from None


def _validated_quotes(quotes: object, source_text: str) -> tuple[str, ...]:
    if (
        isinstance(quotes, (str, bytes))
        or not isinstance(quotes, Sequence)
        or len(quotes) > _MAX_QUOTES
    ):
        raise EvidenceValidationError(
            EvidenceFailureReason.INVALID_DATA,
            "quotes must be a bounded sequence",
        )
    normalized: list[str] = []
    for quote in quotes:
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
