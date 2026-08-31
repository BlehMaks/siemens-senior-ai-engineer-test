"""Pure deterministic structural chunking and bounded context selection."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .contracts import SearchHit
from .documents import (
    ResearchChunk,
    ResearchDocument,
    SourceType,
    build_research_document,
    classify_source,
    normalize_document_text,
)
from .tools import ExtractedBlock, ExtractedDocument

_MAX_DOCUMENTS = 24
_MAX_SELECTED_CHUNKS = 8
_MAX_PUBLIC_QUOTES = 5
_MAX_QUOTE_CHARS = 400
_MAX_CONTEXT_CHARS = 8_000
_MAX_REQUEST_CHARS = 1_000
_TOKEN_PATTERN = re.compile(r"[^\W_]+(?:[-./][^\W_]+)*", flags=re.UNICODE)
_AUTHORITY = {
    SourceType.OFFICIAL_REPORT: 1.0,
    SourceType.REGULATORY_FILING: 0.98,
    SourceType.GOVERNMENT: 0.95,
    SourceType.OFFICIAL_DOCUMENTATION: 0.92,
    SourceType.ACADEMIC: 0.86,
    SourceType.MAJOR_MEDIA: 0.75,
    SourceType.TECHNICAL_ARTICLE: 0.55,
    SourceType.FORUM: 0.25,
    SourceType.UNKNOWN: 0.15,
}


class RetrievalFailureReason(StrEnum):
    INVALID_INPUT = "invalid_input"
    NO_CONTEXT = "no_context"
    INVALID_CONTEXT = "invalid_context"
    SOURCE_MISMATCH = "source_mismatch"


class RetrievalError(RuntimeError):
    def __init__(self, reason: RetrievalFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RankingFeatures:
    chunk_id: str
    lexical: float
    exact_term: float
    authority: float
    freshness: float
    final: float

    def __post_init__(self) -> None:
        if re.fullmatch(r"chunk-[a-f0-9]{24}", self.chunk_id) is None:
            raise ValueError("ranking chunk ID is invalid")
        for value in (
            self.lexical,
            self.exact_term,
            self.authority,
            self.freshness,
            self.final,
        ):
            if type(value) is not float or not 0.0 <= value <= 10.0:
                raise ValueError("ranking feature is outside its bound")


@dataclass(frozen=True, slots=True)
class SelectedContext:
    chunks: tuple[ResearchChunk, ...]
    chunk_ids: tuple[str, ...]
    quotes: tuple[str, ...]
    total_characters: int
    score_components: tuple[RankingFeatures, ...]
    context_hash: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.chunks) <= _MAX_SELECTED_CHUNKS:
            raise ValueError("selected context must contain bounded chunks")
        if self.chunk_ids != tuple(chunk.chunk_id for chunk in self.chunks):
            raise ValueError("selected context chunk IDs do not match")
        if not 1 <= len(self.quotes) <= _MAX_PUBLIC_QUOTES:
            raise ValueError("selected context must contain bounded quotes")
        if any(not quote or len(quote) > _MAX_QUOTE_CHARS for quote in self.quotes):
            raise ValueError("selected context quote is outside its bound")
        if self.total_characters != sum(len(quote) for quote in self.quotes):
            raise ValueError("selected context character count does not match")
        if self.total_characters > _MAX_CONTEXT_CHARS:
            raise ValueError("selected context exceeds its character bound")
        if tuple(score.chunk_id for score in self.score_components) != self.chunk_ids:
            raise ValueError("selected context scores do not match chunks")
        if re.fullmatch(r"[a-f0-9]{64}", self.context_hash) is None:
            raise ValueError("selected context hash is invalid")

    @property
    def total_chars(self) -> int:
        return self.total_characters


def retrieve_context(
    request_text: str,
    hit: SearchHit,
    extracted: ExtractedDocument,
    *,
    retrieved_at: datetime | None = None,
    top_k: int = 5,
    max_context_chars: int = 2_000,
    max_chunk_chars: int = 1_200,
) -> SelectedContext:
    """Select context directly from one search hit and extracted document."""

    try:
        document = build_research_document(hit, extracted, retrieved_at=retrieved_at)
    except Exception:
        raise RetrievalError(
            RetrievalFailureReason.INVALID_INPUT,
            "research document failed validation",
        ) from None
    return select_context(
        request_text,
        (document,),
        top_k=top_k,
        max_context_chars=max_context_chars,
        max_chunk_chars=max_chunk_chars,
    )


def select_context(
    request_text: str,
    documents: Sequence[ResearchDocument],
    *,
    top_k: int = 5,
    max_context_chars: int = 2_000,
    max_chunk_chars: int = 1_200,
) -> SelectedContext:
    """Rank chunks across bounded documents and remove exact content mirrors."""

    try:
        request = _request(request_text)
        _validate_bounds(
            top_k=top_k,
            max_context_chars=max_context_chars,
            max_chunk_chars=max_chunk_chars,
        )
        if isinstance(documents, (str, bytes)) or not isinstance(documents, Sequence):
            raise ValueError
        materialized = tuple(documents)
        if not 1 <= len(materialized) <= _MAX_DOCUMENTS or any(
            type(document) is not ResearchDocument for document in materialized
        ):
            raise ValueError
    except Exception:
        raise RetrievalError(
            RetrievalFailureReason.INVALID_INPUT,
            "retrieval input failed validation",
        ) from None

    by_content: dict[str, ResearchChunk] = {}
    for document in materialized:
        for chunk in chunk_document(request, document, max_chunk_chars=max_chunk_chars):
            previous = by_content.get(chunk.content_hash)
            if previous is None or _rank_key(chunk) < _rank_key(previous):
                by_content[chunk.content_hash] = chunk
    ranked = sorted(by_content.values(), key=_rank_key)
    selected = tuple(ranked[:top_k])
    if not selected:
        raise RetrievalError(
            RetrievalFailureReason.NO_CONTEXT,
            "retrieval produced no context",
        )
    quotes: list[str] = []
    remaining = max_context_chars
    for chunk in selected[:_MAX_PUBLIC_QUOTES]:
        quote = _bounded_quote(chunk.text, min(remaining, _MAX_QUOTE_CHARS))
        if quote:
            quotes.append(quote)
            remaining -= len(quote)
        if remaining <= 0:
            break
    if not quotes:
        raise RetrievalError(
            RetrievalFailureReason.NO_CONTEXT,
            "retrieval produced no bounded quotes",
        )
    scores = tuple(_features(chunk) for chunk in selected)
    result = SelectedContext(
        chunks=selected,
        chunk_ids=tuple(chunk.chunk_id for chunk in selected),
        quotes=tuple(quotes),
        total_characters=sum(len(quote) for quote in quotes),
        score_components=scores,
        context_hash=_context_hash(selected, tuple(quotes)),
    )
    validate_selected_context(result)
    return result


def chunk_document(
    request_text: str,
    document: ResearchDocument,
    *,
    max_chunk_chars: int = 1_200,
) -> tuple[ResearchChunk, ...]:
    """Structurally chunk, exact-deduplicate, and score one document."""

    request = _request(request_text)
    if type(document) is not ResearchDocument:
        raise RetrievalError(
            RetrievalFailureReason.INVALID_INPUT,
            "research document has an invalid type",
        )
    if (
        isinstance(max_chunk_chars, bool)
        or not isinstance(max_chunk_chars, int)
        or not 80 <= max_chunk_chars <= 1_200
    ):
        raise RetrievalError(
            RetrievalFailureReason.INVALID_INPUT,
            "max_chunk_chars is outside its bound",
        )
    query_tokens = frozenset(_tokens(request))
    exact_terms = frozenset(token for token in query_tokens if _is_exact_term(token))
    authority = _AUTHORITY[document.source_type]
    freshness = _freshness(
        document.updated_at or document.published_at,
        retrieved_at=document.retrieved_at,
    )
    chunks: list[ResearchChunk] = []
    seen_hashes: set[str] = set()
    ordinal = 0
    for block in document.blocks:
        for text in _split_block(block, max_chars=max_chunk_chars):
            normalized = normalize_document_text(text)
            content_hash = hashlib.sha256(normalized.encode()).hexdigest()
            if content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)
            ordinal += 1
            lexical, exact = _lexical_scores(
                query_tokens,
                exact_terms,
                chunk_text=normalized,
                title=document.title,
            )
            final = round(
                min(10.0, lexical * 5.0 + exact * 2.0 + authority * 2.0 + freshness),
                6,
            )
            digest = hashlib.sha256(
                (
                    f"{document.document_id}\n{content_hash}\n{block.page_number}\n"
                    f"{block.section}\n{block.table_index}"
                ).encode()
            ).hexdigest()
            chunks.append(
                ResearchChunk(
                    chunk_id=f"chunk-{digest[:24]}",
                    document_id=document.document_id,
                    canonical_url=document.canonical_url,
                    title=document.title,
                    source_type=document.source_type,
                    content_hash=content_hash,
                    ordinal=ordinal,
                    page_number=block.page_number,
                    section=block.section,
                    table_index=block.table_index,
                    published_at=document.published_at,
                    updated_at=document.updated_at,
                    retrieved_at=document.retrieved_at,
                    text=normalized,
                    lexical_score=lexical,
                    exact_term_score=exact,
                    authority_score=authority,
                    freshness_score=freshness,
                    final_score=final,
                )
            )
    if not chunks:
        raise RetrievalError(
            RetrievalFailureReason.NO_CONTEXT,
            "document produced no chunks",
        )
    return tuple(sorted(chunks, key=_rank_key))


def validate_selected_context(context: SelectedContext) -> None:
    """Recompute provenance and ranking projections at the runner boundary."""

    try:
        if (
            type(context) is not SelectedContext
            or type(context.chunks) is not tuple
            or type(context.chunk_ids) is not tuple
            or type(context.quotes) is not tuple
            or type(context.score_components) is not tuple
        ):
            raise ValueError
        if context.chunk_ids != tuple(chunk.chunk_id for chunk in context.chunks):
            raise ValueError
        if context.score_components != tuple(
            _features(chunk) for chunk in context.chunks
        ):
            raise ValueError
        for index, quote in enumerate(context.quotes):
            if quote not in " ".join(context.chunks[index].text.split()):
                raise ValueError
        if context.total_characters != sum(len(quote) for quote in context.quotes):
            raise ValueError
        if context.context_hash != _context_hash(context.chunks, context.quotes):
            raise ValueError
    except Exception:
        raise RetrievalError(
            RetrievalFailureReason.INVALID_CONTEXT,
            "selected context failed its integrity check",
        ) from None


def _features(chunk: ResearchChunk) -> RankingFeatures:
    return RankingFeatures(
        chunk_id=chunk.chunk_id,
        lexical=chunk.lexical_score,
        exact_term=chunk.exact_term_score,
        authority=chunk.authority_score,
        freshness=chunk.freshness_score,
        final=chunk.final_score,
    )


def _rank_key(chunk: ResearchChunk) -> tuple[float, float, float, float, int, str]:
    return (
        -chunk.final_score,
        -chunk.lexical_score,
        -chunk.authority_score,
        -chunk.freshness_score,
        chunk.ordinal,
        chunk.chunk_id,
    )


def _split_block(block: ExtractedBlock, *, max_chars: int) -> tuple[str, ...]:
    text = normalize_document_text(block.text)
    if len(text) <= max_chars:
        return (text,)
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(paragraphs) == 1:
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    if len(paragraphs) == 1:
        paragraphs = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
            if part.strip()
        ]
    chunks: list[str] = []
    current = ""
    for part in paragraphs:
        for bounded_part in _hard_wrap(part, max_chars=max_chars):
            candidate = (
                f"{current}\n{bounded_part}".strip() if current else bounded_part
            )
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = bounded_part
    if current:
        chunks.append(current)
    return tuple(chunks)


def _hard_wrap(value: str, *, max_chars: int) -> tuple[str, ...]:
    if len(value) <= max_chars:
        return (value,)
    result: list[str] = []
    remaining = value
    while len(remaining) > max_chars:
        boundary = remaining.rfind(" ", 0, max_chars + 1)
        if boundary < max_chars // 2:
            boundary = max_chars
        result.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        result.append(remaining)
    return tuple(result)


def _lexical_scores(
    query_tokens: frozenset[str],
    exact_terms: frozenset[str],
    *,
    chunk_text: str,
    title: str,
) -> tuple[float, float]:
    chunk_tokens = frozenset(_tokens(chunk_text))
    title_tokens = frozenset(_tokens(title))
    overlap = len(query_tokens & chunk_tokens)
    title_overlap = len(query_tokens & title_tokens)
    lexical = round(min(1.0, (overlap + 0.25 * title_overlap) / len(query_tokens)), 6)
    exact = (
        len(exact_terms & chunk_tokens) / len(exact_terms)
        if exact_terms
        else float(query_tokens <= chunk_tokens)
    )
    return lexical, round(min(1.0, exact), 6)


def _freshness(timestamp: datetime | None, *, retrieved_at: datetime | None) -> float:
    if timestamp is None or (retrieved_at is not None and timestamp > retrieved_at):
        return 0.0
    # Absolute recency is deterministic and never mistakes retrieval time for freshness.
    return round(min(1.0, max(0.0, (timestamp.year - 1970) / 130.0)), 6)


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _TOKEN_PATTERN.finditer(value))


def _is_exact_term(value: str) -> bool:
    return any(character.isdigit() for character in value) or len(value) >= 12


def _bounded_quote(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    rendered = " ".join(value.split())
    if len(rendered) <= limit:
        return rendered
    boundary = rendered.rfind(" ", 0, limit + 1)
    if boundary < max(1, limit // 2):
        boundary = limit
    return rendered[:boundary].rstrip()


def _context_hash(chunks: Sequence[ResearchChunk], quotes: Sequence[str]) -> str:
    material = [f"{chunk.chunk_id}:{chunk.final_score:.6f}" for chunk in chunks]
    material.extend(f"quote:{quote}" for quote in quotes)
    return hashlib.sha256("\n".join(material).encode()).hexdigest()


def _request(value: object) -> str:
    if type(value) is not str:
        raise ValueError("request text must be plain text")
    rendered = " ".join(value.split())
    if not rendered or len(rendered) > _MAX_REQUEST_CHARS or not _tokens(rendered):
        raise ValueError("request text is empty or exceeds its bound")
    return rendered


def _validate_bounds(
    *, top_k: int, max_context_chars: int, max_chunk_chars: int
) -> None:
    if (
        isinstance(top_k, bool)
        or not isinstance(top_k, int)
        or not 1 <= top_k <= _MAX_SELECTED_CHUNKS
    ):
        raise ValueError("top_k is outside its bound")
    if (
        isinstance(max_context_chars, bool)
        or not isinstance(max_context_chars, int)
        or not 80 <= max_context_chars <= _MAX_CONTEXT_CHARS
    ):
        raise ValueError("max_context_chars is outside its bound")
    if (
        isinstance(max_chunk_chars, bool)
        or not isinstance(max_chunk_chars, int)
        or not 80 <= max_chunk_chars <= 1_200
    ):
        raise ValueError("max_chunk_chars is outside its bound")


__all__ = [
    "RankingFeatures",
    "ResearchChunk",
    "ResearchDocument",
    "RetrievalError",
    "RetrievalFailureReason",
    "SelectedContext",
    "SourceType",
    "build_research_document",
    "chunk_document",
    "classify_source",
    "retrieve_context",
    "select_context",
    "validate_selected_context",
]
