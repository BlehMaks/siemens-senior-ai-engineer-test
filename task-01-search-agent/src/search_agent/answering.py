"""Deterministic validation for citation-grounded answers."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .contracts import ScopedAnswer
from .evidence import EvidenceRecord, EvidenceValidationError, validate_record


class AbstentionReason(StrEnum):
    """Typed reasons an answer is withheld instead of rendered."""

    NO_EVIDENCE = "no_evidence"
    INVALID_EVIDENCE = "invalid_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    STALE_EVIDENCE = "stale_evidence"
    UNKNOWN_CITATION = "unknown_citation"
    DUPLICATE_CITATION = "duplicate_citation"
    URL_MISMATCH = "url_mismatch"
    CLAIM_NOT_IN_ANSWER = "claim_not_in_answer"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    UNCITED_CONTENT = "uncited_content"
    DUPLICATE_SOURCE = "duplicate_source"
    INSUFFICIENT_SOURCE_DIVERSITY = "insufficient_source_diversity"
    INVALID_ANSWER = "invalid_answer"


class AnswerAbstained(RuntimeError):
    def __init__(self, reason: AbstentionReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AnswerValidator:
    """Return an existing answer only after every citation is verified."""

    minimum_sources: int = 1
    max_evidence_age: timedelta = timedelta(days=30)

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_sources, bool)
            or not isinstance(self.minimum_sources, int)
            or self.minimum_sources < 1
        ):
            raise ValueError("minimum_sources must be a positive integer")
        if not isinstance(
            self.max_evidence_age, timedelta
        ) or self.max_evidence_age <= timedelta(0):
            raise ValueError("max_evidence_age must be positive")

    def validate(
        self,
        answer: ScopedAnswer,
        evidence: Sequence[EvidenceRecord],
        *,
        now: datetime | None = None,
    ) -> ScopedAnswer:
        checked_answer = self._validate_answer_contract(answer)
        if not evidence:
            raise AnswerAbstained(
                AbstentionReason.NO_EVIDENCE,
                "an answer cannot be rendered without evidence",
            )
        checked_now = _utc_now(now)
        records = self._index_records(evidence)

        answer_text = _normalize(checked_answer.answer_text)
        cited_urls: list[str] = []
        cited_hashes: set[str] = set()
        cited_claims: list[str] = []
        for citation in checked_answer.citations:
            record = records.get(citation.evidence_id)
            if record is None:
                raise AnswerAbstained(
                    AbstentionReason.UNKNOWN_CITATION,
                    "citation references unknown evidence",
                )
            if record.retrieved_at > checked_now:
                raise AnswerAbstained(
                    AbstentionReason.INVALID_EVIDENCE,
                    "citation evidence has a future retrieval time",
                )
            if checked_now - record.retrieved_at > self.max_evidence_age:
                raise AnswerAbstained(
                    AbstentionReason.STALE_EVIDENCE,
                    "citation references stale evidence",
                )
            if str(citation.source_url) != record.source_url:
                raise AnswerAbstained(
                    AbstentionReason.URL_MISMATCH,
                    "citation URL does not match stored evidence provenance",
                )

            claim = _normalize(citation.claim)
            if claim not in answer_text:
                raise AnswerAbstained(
                    AbstentionReason.CLAIM_NOT_IN_ANSWER,
                    "citation claim does not occur in the answer",
                )
            # Exact normalized containment is deliberately conservative. It is
            # explainable and cannot turn model similarity into fabricated support.
            support_texts = (
                record.source_text,
                record.public.summary,
                *record.public.quotes,
            )
            if not any(
                _contains_exact_text(support, claim) for support in support_texts
            ):
                raise AnswerAbstained(
                    AbstentionReason.UNSUPPORTED_CLAIM,
                    "citation claim does not occur in its evidence",
                )
            cited_claims.append(claim)
            cited_urls.append(record.source_url)
            cited_hashes.add(record.content_hash)

        # The baseline renderer accepts only cited claims in citation order. This
        # makes uncited model additions impossible instead of guessing support.
        if answer_text != " ".join(cited_claims):
            raise AnswerAbstained(
                AbstentionReason.UNCITED_CONTENT,
                "answer contains content outside its cited claims",
            )

        if self.minimum_sources > 1 and len(cited_urls) != len(set(cited_urls)):
            raise AnswerAbstained(
                AbstentionReason.DUPLICATE_SOURCE,
                "duplicate canonical sources cannot satisfy source diversity",
            )
        # Identical content mirrored under several URLs is one independent source.
        independent_sources = min(len(set(cited_urls)), len(cited_hashes))
        if independent_sources < self.minimum_sources:
            raise AnswerAbstained(
                AbstentionReason.INSUFFICIENT_SOURCE_DIVERSITY,
                "answer does not meet minimum source diversity",
            )
        return answer

    @staticmethod
    def _validate_answer_contract(answer: ScopedAnswer) -> ScopedAnswer:
        try:
            if isinstance(answer.citations, tuple):
                citation_ids = [citation.evidence_id for citation in answer.citations]
                if len(citation_ids) != len(set(citation_ids)):
                    raise AnswerAbstained(
                        AbstentionReason.DUPLICATE_CITATION,
                        "citation evidence ids must be unique",
                    )
            return ScopedAnswer.model_validate(
                answer.model_dump(mode="python", warnings="error"),
                strict=True,
            )
        except AnswerAbstained:
            raise
        except (AttributeError, TypeError, ValueError):
            raise AnswerAbstained(
                AbstentionReason.INVALID_ANSWER,
                "answer failed strict contract validation",
            ) from None

    @staticmethod
    def _index_records(
        evidence: Sequence[EvidenceRecord],
    ) -> dict[str, EvidenceRecord]:
        records: dict[str, EvidenceRecord] = {}
        hashes: dict[str, str] = {}
        for record in evidence:
            if not isinstance(record, EvidenceRecord):
                raise AnswerAbstained(
                    AbstentionReason.INVALID_EVIDENCE,
                    "evidence collection contains an invalid record",
                )
            try:
                evidence_id = record.evidence_id
                content_hash = record.content_hash
                source_text = record.source_text
            except (AttributeError, TypeError, ValueError):
                raise AnswerAbstained(
                    AbstentionReason.INVALID_EVIDENCE,
                    "evidence collection contains an invalid record",
                ) from None
            existing = records.get(evidence_id)
            if existing is not None:
                raise AnswerAbstained(
                    AbstentionReason.CONFLICTING_EVIDENCE,
                    "evidence collection contains a duplicate evidence id",
                )
            prior_text = hashes.get(content_hash)
            if prior_text is not None and prior_text != source_text:
                raise AnswerAbstained(
                    AbstentionReason.CONFLICTING_EVIDENCE,
                    "evidence collection contains a conflicting content hash",
                )
            try:
                validate_record(record)
            except EvidenceValidationError:
                raise AnswerAbstained(
                    AbstentionReason.INVALID_EVIDENCE,
                    "evidence collection contains an invalid record",
                ) from None
            records[evidence_id] = record
            hashes[content_hash] = source_text
        return records


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _contains_exact_text(source: str, claim: str) -> bool:
    start = source.find(claim)
    while start >= 0:
        end = start + len(claim)
        left_boundary = (
            start == 0
            or not _is_word_character(claim[0])
            or not _is_word_character(source[start - 1])
        )
        right_boundary = (
            end == len(source)
            or not _is_word_character(claim[-1])
            or not _is_word_character(source[end])
        )
        if left_boundary and right_boundary:
            return True
        start = source.find(claim, start + 1)
    return False


def _is_word_character(character: str) -> bool:
    category = unicodedata.category(character)
    return character.isalnum() or category.startswith("M") or category == "Pc"


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() != timedelta(0)
    ):
        raise ValueError("now must be timezone-aware UTC")
    return current.astimezone(UTC)
