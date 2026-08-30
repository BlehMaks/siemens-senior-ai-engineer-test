from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AnyHttpUrl, TypeAdapter

from search_agent import (
    ExtractedBlock,
    ExtractedDocument,
    FetchedDocument,
    LocalExtractor,
    SearchHit,
    SourceType,
    build_research_document,
    retrieve_context,
    select_context,
    validate_selected_context,
)

_URL = TypeAdapter(AnyHttpUrl)
_NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
_TITLE = "Siemens Sustainability Report"
_FIXTURES = Path(__file__).parent / "fixtures" / "retrieval"


def _hit(
    url: str = "https://www.siemens.com/reports/sustainability.pdf",
    *,
    snippet: str = "Published 2025-12-01",
) -> SearchHit:
    return SearchHit(
        title=_TITLE,
        url=_URL.validate_python(url),
        snippet=snippet,
        rank=1,
    )


def _document(
    *,
    url: str = "https://www.siemens.com/reports/sustainability.pdf",
    text: str | None = None,
    blocks: tuple[ExtractedBlock, ...] | None = None,
    published_at: datetime | None = None,
) -> ExtractedDocument:
    overview = "Overview and general background for the annual report."
    late_fact = "The 2025 Scope 3 emissions were 14.7 million tonnes CO2e."
    rendered = text or f"{overview}\n\n{late_fact}"
    return ExtractedDocument(
        canonical_url=url,
        title=_TITLE,
        text=rendered,
        media_type="application/pdf",
        blocks=blocks
        or (
            ExtractedBlock(text=overview, page_number=1, section="Overview"),
            ExtractedBlock(
                text=late_fact,
                page_number=42,
                section="Scope 3 Emissions",
                table_index=1,
            ),
        ),
        published_at=published_at,
    )


def test_late_pdf_fact_is_selected_with_exact_page_and_table_provenance() -> None:
    first = retrieve_context(
        "What were Siemens Scope 3 emissions in 2025?",
        _hit(),
        _document(),
        retrieved_at=_NOW,
    )
    second = retrieve_context(
        "What were Siemens Scope 3 emissions in 2025?",
        _hit(),
        _document(),
        retrieved_at=_NOW,
    )

    assert first == second
    assert first.chunks[0].page_number == 42
    assert first.chunks[0].table_index == 1
    assert first.quotes[0] == (
        "The 2025 Scope 3 emissions were 14.7 million tonnes CO2e."
    )
    assert first.total_characters == sum(map(len, first.quotes))
    assert len(first.context_hash) == 64
    validate_selected_context(first)


def test_frozen_pdf_pipeline_selects_the_late_page_fact() -> None:
    body = base64.b64decode(
        (_FIXTURES / "late_fact_report.pdf.b64").read_text().strip(),
        validate=True,
    )
    extracted = LocalExtractor().extract(
        FetchedDocument(
            canonical_url="https://www.siemens.com/reports/sustainability.pdf",
            content_type="application/pdf",
            body=body,
        )
    )

    context = retrieve_context(
        "What were Siemens Scope 3 emissions in 2025?",
        _hit(),
        extracted,
        retrieved_at=_NOW,
    )

    assert context.chunks[0].page_number == 2
    assert "14.7 million tonnes CO2e" in context.quotes[0]
    assert any(chunk.table_index is not None for chunk in context.chunks)


def test_document_and_chunk_ids_ignore_incidental_whitespace() -> None:
    normalized = build_research_document(_hit(), _document(), retrieved_at=_NOW)
    spaced = _document(
        text=(
            "  Overview and general background for the annual report.\n\n"
            "The 2025 Scope 3 emissions were 14.7 million tonnes CO2e.  "
        ),
    )
    rebuilt = build_research_document(_hit(), spaced, retrieved_at=_NOW)

    assert normalized.document_id == rebuilt.document_id
    assert normalized.content_hash == rebuilt.content_hash


def test_exact_duplicate_content_keeps_the_more_authoritative_source() -> None:
    text = "Siemens Scope 3 emissions were 14.7 million tonnes CO2e in 2025."
    official_hit = _hit()
    mirror_hit = _hit("https://mirror.example/reports/sustainability.pdf")
    official = build_research_document(
        official_hit,
        _document(
            text=text,
            blocks=(ExtractedBlock(text=text, page_number=2),),
        ),
        retrieved_at=_NOW,
    )
    mirror = build_research_document(
        mirror_hit,
        _document(
            url="https://mirror.example/reports/sustainability.pdf",
            text=text,
            blocks=(ExtractedBlock(text=text, page_number=2),),
        ),
        retrieved_at=_NOW,
    )

    context = select_context(
        "Siemens Scope 3 emissions 2025",
        (mirror, official),
        top_k=5,
    )

    assert len(context.chunks) == 1
    assert context.chunks[0].canonical_url == str(official_hit.url)
    assert context.chunks[0].source_type is SourceType.OFFICIAL_REPORT
    assert context.score_components[0].authority == 1.0


def test_authority_freshness_and_lexical_components_have_stable_ordering() -> None:
    primary_text = "Siemens 2025 Scope 3 emissions result is 14.7 million tonnes."
    secondary_text = "Siemens 2024 Scope 3 emissions commentary is secondary."
    primary = build_research_document(
        _hit(snippet="Published 2026-01-15"),
        _document(
            text=primary_text,
            blocks=(ExtractedBlock(text=primary_text, page_number=3),),
            published_at=datetime(2026, 1, 15, tzinfo=UTC),
        ),
        retrieved_at=_NOW,
    )
    secondary_url = "https://analysis.example/siemens-emissions"
    secondary = build_research_document(
        _hit(secondary_url, snippet="Published 2024-01-15"),
        _document(
            url=secondary_url,
            text=secondary_text,
            blocks=(ExtractedBlock(text=secondary_text),),
            published_at=datetime(2024, 1, 15, tzinfo=UTC),
        ),
        retrieved_at=_NOW,
    )

    context = select_context(
        "Siemens 2025 Scope 3 emissions 14.7 million tonnes",
        (secondary, primary),
        top_k=2,
    )

    assert context.chunks[0].document_id == primary.document_id
    assert context.score_components[0].lexical > context.score_components[1].lexical
    assert context.score_components[0].authority > context.score_components[1].authority
    assert context.score_components[0].freshness > context.score_components[1].freshness
