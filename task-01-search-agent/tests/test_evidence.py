from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import AnyHttpUrl, TypeAdapter

from search_agent.contracts import SearchHit
from search_agent.evidence import (
    EvidenceFailureReason,
    EvidenceValidationError,
    build_evidence,
    validate_record,
)
from search_agent.tools import ExtractedDocument

_URL = TypeAdapter(AnyHttpUrl)
_NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


class _HostileQuotes(tuple[str, ...]):
    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("hostile quote iteration")


def _hit(
    *,
    url: str = "https://example.com/report#results",
    title: str = "Siemens sustainability report",
) -> SearchHit:
    return SearchHit(
        title=title,
        url=_URL.validate_python(url),
        snippet="Siemens published its sustainability results.",
        rank=1,
    )


def _document(
    *,
    url: str = "https://example.com/report",
    title: str | None = "Siemens sustainability report",
    text: str = "Siemens reduced emissions in 2025. The target remains active.",
) -> ExtractedDocument:
    return ExtractedDocument(canonical_url=url, title=title, text=text)


def test_builds_deterministic_immutable_evidence_from_exact_provenance() -> None:
    first = build_evidence(
        _hit(),
        _document(text="  Siemens reduced\n emissions in 2025.  "),
        retrieved_at=_NOW,
        quotes=("Siemens reduced emissions in 2025.",),
        now=_NOW,
    )
    second = build_evidence(
        _hit(),
        _document(text="Siemens reduced emissions in 2025."),
        retrieved_at=_NOW,
        quotes=("Siemens reduced emissions in 2025.",),
        now=_NOW,
    )

    assert first.evidence_id == second.evidence_id
    assert first.evidence_id.startswith("ev-")
    assert len(first.content_hash) == 64
    assert len(first.title_provenance_hash) == 64
    assert first.source_url == "https://example.com/report"
    assert first.source_text == "Siemens reduced emissions in 2025."
    assert first.public.summary == first.source_text
    assert first.public.quotes == ("Siemens reduced emissions in 2025.",)
    validate_record(first)


@pytest.mark.parametrize(
    ("hit", "document"),
    [
        (_hit(), _document(url="https://other.example/report")),
        (_hit(), _document(title="Different report")),
    ],
)
def test_rejects_url_and_title_provenance_mismatch(
    hit: SearchHit, document: ExtractedDocument
) -> None:
    with pytest.raises(EvidenceValidationError) as error:
        build_evidence(hit, document, retrieved_at=_NOW, now=_NOW)

    assert error.value.reason is EvidenceFailureReason.SOURCE_MISMATCH


@pytest.mark.parametrize(
    "text",
    ["", "\u200bhidden", "x" * 100_001],
)
def test_rejects_empty_malformed_and_oversize_source_text(text: str) -> None:
    with pytest.raises(EvidenceValidationError) as error:
        build_evidence(_hit(), _document(text=text), retrieved_at=_NOW, now=_NOW)

    assert error.value.reason is EvidenceFailureReason.INVALID_DATA


def test_rejects_fabricated_and_oversize_quotes() -> None:
    with pytest.raises(EvidenceValidationError) as fabricated:
        build_evidence(
            _hit(),
            _document(),
            retrieved_at=_NOW,
            quotes=("Siemens made an unsupported claim.",),
            now=_NOW,
        )
    with pytest.raises(EvidenceValidationError) as oversize:
        build_evidence(
            _hit(),
            _document(text="x" * 500),
            retrieved_at=_NOW,
            quotes=("x" * 401,),
            now=_NOW,
        )

    assert fabricated.value.reason is EvidenceFailureReason.UNSUPPORTED_QUOTE
    assert oversize.value.reason is EvidenceFailureReason.INVALID_DATA


def test_rejects_stale_future_and_naive_retrieval_times() -> None:
    with pytest.raises(EvidenceValidationError) as stale:
        build_evidence(
            _hit(),
            _document(),
            retrieved_at=_NOW - timedelta(days=31),
            now=_NOW,
        )
    with pytest.raises(EvidenceValidationError) as future:
        build_evidence(
            _hit(),
            _document(),
            retrieved_at=_NOW + timedelta(seconds=1),
            now=_NOW,
        )
    with pytest.raises(EvidenceValidationError) as naive:
        build_evidence(
            _hit(),
            _document(),
            retrieved_at=_NOW.replace(tzinfo=None),
            now=_NOW,
        )

    assert stale.value.reason is EvidenceFailureReason.STALE
    assert future.value.reason is EvidenceFailureReason.INVALID_DATA
    assert naive.value.reason is EvidenceFailureReason.INVALID_DATA


def test_revalidates_hash_and_evidence_id_before_answering() -> None:
    record = build_evidence(_hit(), _document(), retrieved_at=_NOW, now=_NOW)
    object.__setattr__(record, "content_hash", "0" * 64)

    with pytest.raises(EvidenceValidationError) as error:
        validate_record(record)

    assert error.value.reason is EvidenceFailureReason.INVALID_DATA


def test_requires_document_title_and_rejects_public_title_tamper() -> None:
    with pytest.raises(EvidenceValidationError) as missing:
        build_evidence(
            _hit(),
            _document(title=None),
            retrieved_at=_NOW,
            now=_NOW,
        )

    record = build_evidence(_hit(), _document(), retrieved_at=_NOW, now=_NOW)
    object.__setattr__(
        record,
        "public",
        record.public.model_copy(update={"source_title": "Tampered title"}),
    )
    with pytest.raises(EvidenceValidationError) as tampered:
        validate_record(record)

    jointly_tampered = build_evidence(_hit(), _document(), retrieved_at=_NOW, now=_NOW)
    object.__setattr__(jointly_tampered, "source_title", "Tampered title")
    object.__setattr__(
        jointly_tampered,
        "public",
        jointly_tampered.public.model_copy(update={"source_title": "Tampered title"}),
    )
    with pytest.raises(EvidenceValidationError) as joint_tamper:
        validate_record(jointly_tampered)

    assert missing.value.reason is EvidenceFailureReason.INVALID_DATA
    assert tampered.value.reason is EvidenceFailureReason.INVALID_DATA
    assert joint_tamper.value.reason is EvidenceFailureReason.INVALID_DATA


def test_rejects_malformed_quotes_and_constructed_public_fields() -> None:
    with pytest.raises(EvidenceValidationError) as malformed_quotes:
        build_evidence(
            _hit(),
            _document(),
            retrieved_at=_NOW,
            quotes=None,  # type: ignore[arg-type]
            now=_NOW,
        )

    record = build_evidence(_hit(), _document(), retrieved_at=_NOW, now=_NOW)
    object.__setattr__(
        record,
        "public",
        record.public.model_construct(
            evidence_id=record.evidence_id,
            source_url=record.public.source_url,
            source_title="",
            summary="",
            quotes=("",),
        ),
    )
    with pytest.raises(EvidenceValidationError) as malformed_public:
        validate_record(record)

    assert malformed_quotes.value.reason is EvidenceFailureReason.INVALID_DATA
    assert malformed_public.value.reason is EvidenceFailureReason.INVALID_DATA


def test_hostile_quote_container_becomes_typed_invalid_data() -> None:
    with pytest.raises(EvidenceValidationError) as error:
        build_evidence(
            _hit(),
            _document(),
            retrieved_at=_NOW,
            quotes=_HostileQuotes(("supported",)),
            now=_NOW,
        )

    assert error.value.reason is EvidenceFailureReason.INVALID_DATA
