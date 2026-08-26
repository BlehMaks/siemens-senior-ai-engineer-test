from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import AnyHttpUrl, TypeAdapter

from search_agent.answering import AbstentionReason, AnswerAbstained, AnswerValidator
from search_agent.contracts import Citation, ScopedAnswer, SearchHit
from search_agent.evidence import EvidenceRecord, build_evidence
from search_agent.tools import ExtractedDocument

_URL = TypeAdapter(AnyHttpUrl)
_NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


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


@pytest.mark.parametrize("word", ["net\u0301work", "net_work"])
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


def test_rejects_conflicting_duplicate_ids_and_hashes() -> None:
    first = _record()
    duplicate_id = _record(
        url="https://second.example/report",
        text="Different source text.",
    )
    object.__setattr__(
        duplicate_id,
        "public",
        duplicate_id.public.model_copy(update={"evidence_id": first.evidence_id}),
    )

    with pytest.raises(AnswerAbstained) as id_error:
        AnswerValidator().validate(_answer(first), (first, duplicate_id), now=_NOW)

    hash_conflict = _record(url="https://third.example/report")
    object.__setattr__(hash_conflict, "source_text", "Tampered source text.")
    with pytest.raises(AnswerAbstained) as hash_error:
        AnswerValidator().validate(_answer(first), (first, hash_conflict), now=_NOW)

    assert id_error.value.reason is AbstentionReason.CONFLICTING_EVIDENCE
    assert hash_error.value.reason is AbstentionReason.CONFLICTING_EVIDENCE
