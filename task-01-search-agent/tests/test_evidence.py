from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import overload

import pytest
from pydantic import AnyHttpUrl, TypeAdapter

from search_agent.contracts import ExtractedEvidence, SearchHit
from search_agent.documents import build_research_document
from search_agent.evidence import (
    EvidenceFailureReason,
    EvidenceValidationError,
    build_evidence,
    validate_record,
)
from search_agent.retrieval import select_context
from search_agent.tools import ExtractedBlock, ExtractedDocument

_URL = TypeAdapter(AnyHttpUrl)
_NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


class _HostileQuotes(tuple[str, ...]):
    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("hostile quote iteration")


class _UnboundedQuotes(Sequence[str]):
    def __init__(self) -> None:
        self.reads = 0

    def __len__(self) -> int:
        return 6

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[str]: ...

    def __getitem__(self, index: int | slice) -> str | Sequence[str]:
        if isinstance(index, slice):
            return ("supported",)
        self.reads += 1
        if self.reads > 6:
            raise AssertionError("quote materialization exceeded the hard cap")
        return "supported"


class _PublicEvidence(ExtractedEvidence):
    pass


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


def test_builds_evidence_quotes_from_relevant_late_document_chunks() -> None:
    introduction = "This report provides general background and governance details."
    late_fact = "Siemens Scope 3 emissions were 14.7 million tonnes CO2e in 2025."
    document = ExtractedDocument(
        canonical_url="https://example.com/report",
        title="Siemens sustainability report",
        text=f"{introduction}\n\n{late_fact}",
        media_type="application/pdf",
        blocks=(
            ExtractedBlock(text=introduction, page_number=1),
            ExtractedBlock(
                text=late_fact,
                page_number=42,
                section="Scope 3 Emissions",
                table_index=1,
            ),
        ),
    )

    record = build_evidence(
        _hit(),
        document,
        retrieved_at=_NOW,
        request_text="What were Siemens Scope 3 emissions in 2025?",
        now=_NOW,
    )

    assert record.public.summary.startswith("This report provides general background")
    assert record.public.quotes[0] == late_fact
    assert record.selected_context is not None
    assert record.selected_context.chunks[0].page_number == 42
    assert record.selected_context.chunks[0].table_index == 1
    validate_record(record)


def test_rejects_forged_selected_chunk_location_at_both_boundaries() -> None:
    document = ExtractedDocument(
        canonical_url="https://example.com/report",
        title="Siemens sustainability report",
        text="Fresh nonce fact 71e5c4e8.",
        blocks=(ExtractedBlock(text="Fresh nonce fact 71e5c4e8.", page_number=1),),
    )
    research_document = build_research_document(_hit(), document, retrieved_at=_NOW)
    selected = select_context("nonce fact", (research_document,), top_k=1)
    forged = replace(
        selected.chunks[0],
        page_number=999,
        section="Fabricated location",
    )

    with pytest.raises(EvidenceValidationError) as error:
        build_evidence(
            _hit(),
            document,
            retrieved_at=_NOW,
            quotes=(forged.text,),
            selected_chunks=(forged,),
            now=_NOW,
        )

    assert error.value.reason is EvidenceFailureReason.INVALID_DATA

    record = build_evidence(
        _hit(),
        document,
        retrieved_at=_NOW,
        quotes=(selected.chunks[0].text,),
        selected_chunks=selected.chunks,
        now=_NOW,
    )
    object.__setattr__(record, "selected_chunks", (forged,))

    with pytest.raises(EvidenceValidationError) as recheck_error:
        validate_record(record)

    assert recheck_error.value.reason is EvidenceFailureReason.INVALID_DATA


def test_rejects_quote_outside_selected_chunks_at_recheck() -> None:
    first = "Siemens invests in U.S. manufacturing"
    decoy = "Siemens CES 2026 partnership update"
    document = ExtractedDocument(
        canonical_url="https://example.com/report",
        title="Siemens Press",
        text=f"07 August 2026\n{first}\n\n06 August 2026\n{decoy}",
        blocks=(
            ExtractedBlock(text=f"07 August 2026\n{first}", section=first),
            ExtractedBlock(text=f"06 August 2026\n{decoy}", section=decoy),
        ),
    )
    research_document = build_research_document(_hit(), document, retrieved_at=_NOW)
    selected = select_context(
        "Return the exact first listed headline dated 2026",
        (research_document,),
    )
    record = build_evidence(
        _hit(),
        document,
        retrieved_at=_NOW,
        quotes=selected.quotes,
        selected_chunks=selected.chunks,
        now=_NOW,
    )
    tampered = replace(
        record,
        public=record.public.model_copy(update={"quotes": (decoy,)}),
    )

    with pytest.raises(EvidenceValidationError) as error:
        validate_record(tampered)

    assert error.value.reason is EvidenceFailureReason.INVALID_DATA


def test_rejects_url_provenance_mismatch() -> None:
    with pytest.raises(EvidenceValidationError) as error:
        build_evidence(
            _hit(),
            _document(url="https://other.example/report"),
            retrieved_at=_NOW,
            now=_NOW,
        )

    assert error.value.reason is EvidenceFailureReason.SOURCE_MISMATCH


def test_uses_fetched_title_and_falls_back_when_document_has_no_title() -> None:
    fetched_title = build_evidence(
        _hit(title="Search result title | Example"),
        _document(title="Canonical page title"),
        retrieved_at=_NOW,
        now=_NOW,
    )
    search_fallback = build_evidence(
        _hit(title="Search result title | Example"),
        _document(title=None),
        retrieved_at=_NOW,
        now=_NOW,
    )

    assert fetched_title.source_title == "Canonical page title"
    assert fetched_title.public.source_title == "Canonical page title"
    assert search_fallback.source_title == "Search result title | Example"
    validate_record(fetched_title)
    validate_record(search_fallback)


def test_accepts_normalized_multiline_ranked_chunk_provenance() -> None:
    hit = _hit(title="Search result title | Example")
    document = _document(
        title="Canonical page title",
        text="Siemens was founded\nby Werner von Siemens and Johann Georg Halske.",
    )
    research_document = build_research_document(
        hit,
        document,
        retrieved_at=_NOW,
    )
    selected = select_context(
        "Who founded Siemens?",
        (research_document,),
        top_k=1,
    )

    record = build_evidence(
        hit,
        document,
        retrieved_at=_NOW,
        quotes=(selected.chunks[0].text,),
        selected_chunks=selected.chunks,
        now=_NOW,
    )

    assert record.public.quotes == (
        "Siemens was founded by Werner von Siemens and Johann Georg Halske.",
    )
    validate_record(record)


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


def test_rejects_public_title_tamper() -> None:
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


def test_quote_materialization_stops_at_the_first_excess_item() -> None:
    quotes = _UnboundedQuotes()

    with pytest.raises(EvidenceValidationError) as error:
        build_evidence(
            _hit(),
            _document(text="supported"),
            retrieved_at=_NOW,
            quotes=quotes,
            now=_NOW,
        )

    assert error.value.reason is EvidenceFailureReason.INVALID_DATA
    assert quotes.reads == 6


@pytest.mark.parametrize("public_kind", ["model", "quotes"])
def test_record_integrity_rejects_public_type_subclasses(public_kind: str) -> None:
    record = build_evidence(
        _hit(),
        _document(text="supported"),
        retrieved_at=_NOW,
        quotes=("supported",),
        now=_NOW,
    )
    payload = record.public.model_dump(mode="python")
    public: ExtractedEvidence
    if public_kind == "model":
        public = _PublicEvidence.model_validate(payload)
    else:
        public = ExtractedEvidence.model_construct(
            **{**payload, "quotes": _HostileQuotes(("supported",))}
        )
    object.__setattr__(record, "public", public)

    with pytest.raises(EvidenceValidationError) as error:
        validate_record(record)

    assert error.value.reason is EvidenceFailureReason.INVALID_DATA
