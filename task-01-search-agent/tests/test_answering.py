from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import AnyHttpUrl, TypeAdapter

from search_agent.answering import AbstentionReason, AnswerAbstained, AnswerValidator
from search_agent.contracts import Citation, ScopedAnswer, SearchHit
from search_agent.documents import build_research_document
from search_agent.evidence import EvidenceRecord, build_evidence
from search_agent.retrieval import select_context
from search_agent.tools import ExtractedBlock, ExtractedDocument

_URL = TypeAdapter(AnyHttpUrl)
_NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


class _HostileTuple(tuple[object, ...]):
    def __iter__(self) -> Iterator[object]:
        raise RuntimeError("hostile citation iteration")


class _HostileEvidence:
    def __iter__(self) -> Iterator[EvidenceRecord]:
        raise RuntimeError("hostile evidence iteration")


class _HostileRecord(EvidenceRecord):
    @property
    def evidence_id(self) -> str:
        raise RuntimeError("hostile evidence id read")


def _record(
    *,
    url: str = "https://example.com/report",
    title: str = "Report",
    text: str = "Siemens reduced emissions in 2025.",
    retrieved_at: datetime = _NOW,
    max_age: timedelta = timedelta(days=30),
) -> EvidenceRecord:
    hit = SearchHit(
        title=title,
        url=_URL.validate_python(url),
        snippet="Relevant search result",
        rank=1,
    )
    document = ExtractedDocument(canonical_url=url, title=title, text=text)
    return build_evidence(
        hit,
        document,
        retrieved_at=retrieved_at,
        now=_NOW,
        max_age=max_age,
    )


def _answer(record: EvidenceRecord, *, claim: str | None = None) -> ScopedAnswer:
    supported_claim = claim or record.source_text
    return ScopedAnswer(
        answer_text=supported_claim,
        citations=(
            Citation(
                claim=supported_claim,
                evidence_id=record.evidence_id,
                source_url=_URL.validate_python(record.source_url),
            ),
        ),
    )


def test_returns_the_existing_contract_only_after_exact_support_validation() -> None:
    record = _record()
    answer = _answer(record)

    validated = AnswerValidator().validate(answer, (record,), now=_NOW)

    assert validated is answer


def test_abstains_without_evidence() -> None:
    record = _record()

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator().validate(_answer(record), (), now=_NOW)

    assert error.value.reason is AbstentionReason.NO_EVIDENCE


def test_rejects_fabricated_citation_id_and_url() -> None:
    record = _record()
    fabricated_id = _answer(record).model_copy(
        update={
            "citations": (
                Citation(
                    claim=record.source_text,
                    evidence_id="ev-fabricated",
                    source_url=_URL.validate_python(record.source_url),
                ),
            )
        }
    )
    fabricated_url = _answer(record).model_copy(
        update={
            "citations": (
                Citation(
                    claim=record.source_text,
                    evidence_id=record.evidence_id,
                    source_url=_URL.validate_python("https://attacker.example/report"),
                ),
            )
        }
    )

    with pytest.raises(AnswerAbstained) as id_error:
        AnswerValidator().validate(fabricated_id, (record,), now=_NOW)
    with pytest.raises(AnswerAbstained) as url_error:
        AnswerValidator().validate(fabricated_url, (record,), now=_NOW)

    assert id_error.value.reason is AbstentionReason.UNKNOWN_CITATION
    assert url_error.value.reason is AbstentionReason.URL_MISMATCH


def test_rejects_claim_absent_from_source_or_answer() -> None:
    record = _record()
    unsupported = _answer(record, claim="Siemens achieved net zero.")
    unrelated_citation = ScopedAnswer(
        answer_text="Siemens reduced emissions in 2025.",
        citations=(
            Citation(
                claim="Different claim",
                evidence_id=record.evidence_id,
                source_url=_URL.validate_python(record.source_url),
            ),
        ),
    )

    with pytest.raises(AnswerAbstained) as unsupported_error:
        AnswerValidator().validate(unsupported, (record,), now=_NOW)
    with pytest.raises(AnswerAbstained) as answer_error:
        AnswerValidator().validate(unrelated_citation, (record,), now=_NOW)

    assert unsupported_error.value.reason is AbstentionReason.UNSUPPORTED_CLAIM
    assert answer_error.value.reason is AbstentionReason.CLAIM_NOT_IN_ANSWER


def test_selected_context_cannot_cite_unselected_source_text() -> None:
    url = "https://press.siemens.com/global/en"
    first = "Siemens invests in U.S. manufacturing"
    decoy = "Siemens CES 2026 partnership update"
    hit = SearchHit(
        title="Siemens Press",
        url=_URL.validate_python(url),
        snippet="Siemens press listings",
        rank=1,
    )
    document = ExtractedDocument(
        canonical_url=url,
        title="Siemens Press",
        text=f"07 August 2026\n{first}\n\n06 August 2026\n{decoy}",
        blocks=(
            ExtractedBlock(
                text=f"07 August 2026\n{first}",
                section=first,
            ),
            ExtractedBlock(
                text=f"06 August 2026\n{decoy}",
                section=decoy,
            ),
        ),
    )
    research_document = build_research_document(hit, document, retrieved_at=_NOW)
    selected = select_context(
        "Return the exact first listed headline dated 2026",
        (research_document,),
    )
    record = build_evidence(
        hit,
        document,
        retrieved_at=_NOW,
        quotes=selected.quotes,
        selected_chunks=selected.chunks,
        now=_NOW,
    )

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator().validate(_answer(record, claim=decoy), (record,), now=_NOW)

    assert error.value.reason is AbstentionReason.UNSUPPORTED_CLAIM


def test_rejects_uncited_answer_content_and_partial_word_support() -> None:
    record = _record(text="The internet service remains available.")
    extra_content = _answer(record).model_copy(
        update={"answer_text": f"{record.source_text} The moon is cheese."}
    )
    partial_word = _answer(record, claim="net")

    with pytest.raises(AnswerAbstained) as uncited_error:
        AnswerValidator().validate(extra_content, (record,), now=_NOW)
    with pytest.raises(AnswerAbstained) as partial_error:
        AnswerValidator().validate(partial_word, (record,), now=_NOW)

    assert uncited_error.value.reason is AbstentionReason.UNCITED_CONTENT
    assert partial_error.value.reason is AbstentionReason.UNSUPPORTED_CLAIM


@pytest.mark.parametrize(
    "word",
    [
        "net\u0301work",
        "work\u0301net",
        "net_work",
        "work_net",
        "net\u203fwork",
        "work\u203fnet",
    ],
)
def test_unicode_word_continuations_do_not_create_a_boundary(word: str) -> None:
    record = _record(text=f"The {word} remains active.")

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator().validate(_answer(record, claim="net"), (record,), now=_NOW)

    assert error.value.reason is AbstentionReason.UNSUPPORTED_CLAIM


@pytest.mark.parametrize("citations", [[], None])
def test_revalidates_constructed_outer_answer_contract(citations: object) -> None:
    record = _record()
    malformed = ScopedAnswer.model_construct(
        answer_text=record.source_text,
        citations=citations,
        assistance=None,
    )

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator().validate(malformed, (record,), now=_NOW)

    assert error.value.reason is AbstentionReason.INVALID_ANSWER


def test_revalidates_constructed_nested_citation_contract() -> None:
    record = _record()
    malformed_citation = Citation.model_construct(
        claim="",
        evidence_id=record.evidence_id,
        source_url=record.public.source_url,
    )
    malformed = ScopedAnswer.model_construct(
        answer_text="",
        citations=(malformed_citation,),
        assistance=None,
    )

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator().validate(malformed, (record,), now=_NOW)

    assert error.value.reason is AbstentionReason.INVALID_ANSWER


def test_rejects_answer_changed_by_validation_and_hostile_containers() -> None:
    record = _record()
    normalized_only = ScopedAnswer.model_construct(
        answer_text=f" {record.source_text} ",
        citations=_answer(record).citations,
        assistance=None,
    )
    hostile_citations = ScopedAnswer.model_construct(
        answer_text=record.source_text,
        citations=_HostileTuple(_answer(record).citations),
        assistance=None,
    )

    for malformed in (normalized_only, hostile_citations):
        with pytest.raises(AnswerAbstained) as error:
            AnswerValidator().validate(malformed, (record,), now=_NOW)
        assert error.value.reason is AbstentionReason.INVALID_ANSWER

    with pytest.raises(AnswerAbstained) as hostile_evidence:
        AnswerValidator().validate(
            _answer(record),
            _HostileEvidence(),  # type: ignore[arg-type]
            now=_NOW,
        )
    assert hostile_evidence.value.reason is AbstentionReason.INVALID_EVIDENCE


def test_unhashable_constructed_id_becomes_typed_abstention() -> None:
    record = _record()
    object.__setattr__(
        record,
        "public",
        record.public.model_construct(
            evidence_id=[],
            source_url=record.public.source_url,
            source_title=record.public.source_title,
            summary=record.public.summary,
            quotes=record.public.quotes,
        ),
    )

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator().validate(_answer(_record()), (record,), now=_NOW)

    assert error.value.reason is AbstentionReason.INVALID_EVIDENCE


def test_record_subclass_is_rejected_before_hostile_property_access() -> None:
    record = _record()
    hostile = _HostileRecord(
        public=record.public,
        retrieved_at=record.retrieved_at,
        source_text=record.source_text,
        content_hash=record.content_hash,
        source_title=record.source_title,
        title_provenance_hash=record.title_provenance_hash,
        selected_chunks_provenance_hash=record.selected_chunks_provenance_hash,
    )

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator().validate(_answer(record), (hostile,), now=_NOW)

    assert error.value.reason is AbstentionReason.INVALID_EVIDENCE


def test_rejects_duplicate_citation_ids_even_if_contract_was_bypassed() -> None:
    record = _record()
    citation = _answer(record).citations[0]
    bypassed = ScopedAnswer.model_construct(
        answer_text=record.source_text,
        citations=(citation, citation),
        assistance=None,
    )

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator().validate(bypassed, (record,), now=_NOW)

    assert error.value.reason is AbstentionReason.DUPLICATE_CITATION


def test_rejects_duplicate_canonical_sources_for_required_diversity() -> None:
    first = _record(text="Siemens reduced emissions in 2025.")
    second = _record(text="Siemens retained its 2030 target.")
    answer = ScopedAnswer(
        answer_text=f"{first.source_text} {second.source_text}",
        citations=(
            Citation(
                claim=first.source_text,
                evidence_id=first.evidence_id,
                source_url=_URL.validate_python(first.source_url),
            ),
            Citation(
                claim=second.source_text,
                evidence_id=second.evidence_id,
                source_url=_URL.validate_python(second.source_url),
            ),
        ),
    )

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator(minimum_sources=2).validate(answer, (first, second), now=_NOW)

    assert error.value.reason is AbstentionReason.DUPLICATE_SOURCE


def test_requires_minimum_independent_source_diversity() -> None:
    record = _record()

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator(minimum_sources=2).validate(
            _answer(record), (record,), now=_NOW
        )

    assert error.value.reason is AbstentionReason.INSUFFICIENT_SOURCE_DIVERSITY


def test_accepts_two_distinct_supported_sources() -> None:
    first = _record()
    second = _record(
        url="https://second.example/report",
        text="The 2030 target remains active.",
    )
    answer = ScopedAnswer(
        answer_text=f"{first.source_text} {second.source_text}",
        citations=(
            Citation(
                claim=first.source_text,
                evidence_id=first.evidence_id,
                source_url=_URL.validate_python(first.source_url),
            ),
            Citation(
                claim=second.source_text,
                evidence_id=second.evidence_id,
                source_url=_URL.validate_python(second.source_url),
            ),
        ),
    )

    assert (
        AnswerValidator(minimum_sources=2).validate(answer, (first, second), now=_NOW)
        is answer
    )


def test_rejects_stale_evidence_at_answer_time() -> None:
    record = _record(
        retrieved_at=_NOW - timedelta(days=31),
        max_age=timedelta(days=60),
    )

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator().validate(_answer(record), (record,), now=_NOW)

    assert error.value.reason is AbstentionReason.STALE_EVIDENCE


def test_rejects_future_retrieval_time_if_record_is_tampered() -> None:
    record = _record()
    object.__setattr__(record, "retrieved_at", _NOW + timedelta(seconds=1))

    with pytest.raises(AnswerAbstained) as error:
        AnswerValidator().validate(_answer(record), (record,), now=_NOW)

    assert error.value.reason is AbstentionReason.INVALID_EVIDENCE


def test_rejects_duplicate_records_and_tampered_hashes() -> None:
    first = _record()
    duplicate_id = _record()

    with pytest.raises(AnswerAbstained) as id_error:
        AnswerValidator().validate(_answer(first), (first, duplicate_id), now=_NOW)

    hash_conflict = _record(url="https://third.example/report")
    object.__setattr__(hash_conflict, "source_text", "Tampered source text.")
    with pytest.raises(AnswerAbstained) as hash_error:
        AnswerValidator().validate(_answer(first), (first, hash_conflict), now=_NOW)

    assert id_error.value.reason is AbstentionReason.CONFLICTING_EVIDENCE
    assert hash_error.value.reason is AbstentionReason.INVALID_EVIDENCE
