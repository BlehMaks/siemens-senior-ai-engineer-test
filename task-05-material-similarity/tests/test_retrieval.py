from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from sklearn.feature_extraction.text import TfidfVectorizer

import material_similarity.retrieval as retrieval
from material_similarity.data import (
    DESCRIPTION_COLUMN,
    MATERIAL_COLUMNS,
    PART_ID_COLUMN,
    MaterialDataError,
)
from material_similarity.retrieval import (
    TOP_K,
    rank_alternatives,
    rank_complete_alternatives,
)


def _material(part_id: str, description: str) -> dict[str, str]:
    material = dict.fromkeys(MATERIAL_COLUMNS, "")
    material[PART_ID_COLUMN] = part_id
    material[DESCRIPTION_COLUMN] = description
    return material


def test_ranking_returns_supported_distinct_self_excluding_top_five() -> None:
    materials = (
        _material("Q", "ceramic fuse 250VAC 6.3A 5x20mm"),
        _material("A", "ceramic fuse 250VAC 6.3A 5x20mm"),
        _material("B", "ceramic fuse 250VAC 5A 5x20mm"),
        _material("C", "glass fuse 250VAC 6.3A 5x20mm"),
        _material("D", "ceramic cartridge fuse 250VAC 6.3A"),
        _material("E", "fast fuse 250VAC 6.3A 5x20mm"),
        _material("F", "slow fuse 125VAC 3A 6x30mm"),
    )

    results = rank_alternatives(materials)
    query = next(result for result in results if result.part_id == "Q")

    assert len(results) == len(materials)
    assert query.status == "ok"
    assert len(query.alternatives) == TOP_K
    assert query.alternatives[0].part_id == "A"
    assert query.alternatives[0].score == 1.0
    assert len({item.part_id for item in query.alternatives}) == TOP_K
    assert "Q" not in {item.part_id for item in query.alternatives}
    assert all(
        item.shared_tokens or item.shared_character_ngrams
        for item in query.alternatives
    )
    assert any("250vac" in item.shared_tokens for item in query.alternatives)


def test_duplicate_descriptions_have_input_order_independent_ties() -> None:
    materials = tuple(
        _material(part_id, "identical ceramic fuse")
        for part_id in ("Q", "F", "B", "E", "A", "D", "C")
    )

    first = rank_alternatives(materials)[0]
    reordered = rank_alternatives((materials[0], *reversed(materials[1:])))[0]

    assert [item.part_id for item in first.alternatives] == ["A", "B", "C", "D", "E"]
    assert first == reordered


def test_duplicate_part_ids_are_rejected_at_the_shared_catalog_boundary() -> None:
    materials = (
        _material("A", "fuse alpha"),
        _material("A", "fuse beta"),
    )

    with pytest.raises(MaterialDataError, match="PART_ID contains duplicates"):
        rank_alternatives(materials)


def test_blank_descriptions_abstain_without_affecting_other_records() -> None:
    materials = (
        _material("BLANK", "  "),
        *(_material(str(index), f"ceramic fuse family {index}") for index in range(6)),
    )

    results = rank_alternatives(materials)
    blank = results[0]

    assert len(results) == len(materials)
    assert blank.status == "insufficient_description"
    assert blank.alternatives == ()
    assert all(result.status == "ok" for result in results[1:])
    assert all(len(result.alternatives) == TOP_K for result in results[1:])


def test_complete_ranking_fills_blank_descriptions_with_labeled_field_evidence() -> (
    None
):
    blank = _material("BLANK", "")
    blank["Fuse Material"] = "Ceramic"
    candidates = []
    for index in range(6):
        candidate = _material(str(index), f"fuse family {index}")
        candidate["Fuse Material"] = "Ceramic"
        candidates.append(candidate)

    result = rank_complete_alternatives((blank, *candidates))[0]

    assert result.status == "ok"
    assert len(result.alternatives) == TOP_K
    assert all(item.method == "structured_fallback" for item in result.alternatives)
    assert all(item.confidence == "field_supported" for item in result.alternatives)
    assert all("Fuse Material" in item.shared_fields for item in result.alternatives)


def test_complete_ranking_uses_missingness_only_for_data_empty_rows() -> None:
    materials = tuple(_material(part_id, "") for part_id in "QABCDEF")

    first = rank_complete_alternatives(materials)[0]
    reordered = rank_complete_alternatives((materials[0], *reversed(materials[1:])))[0]

    assert first.status == "ok"
    assert [item.part_id for item in first.alternatives] == ["A", "B", "C", "D", "E"]
    assert first == reordered
    assert all(item.method == "structured_fallback" for item in first.alternatives)
    assert all(item.confidence == "missingness_only" for item in first.alternatives)
    assert all(
        item.shared_fields and item.shared_fields[0].startswith("missing:")
        for item in first.alternatives
    )


def test_sparse_vocabulary_does_not_fabricate_zero_similarity_results() -> None:
    materials = tuple(
        _material(part_id, description)
        for part_id, description in zip(
            "ABCDEF", ("!", "?", "#", "$", "%", "&"), strict=True
        )
    )

    results = rank_alternatives(materials)

    assert all(result.status == "insufficient_candidates" for result in results)
    assert all(result.alternatives == () for result in results)


def test_result_dataclasses_serialize_to_stable_json() -> None:
    materials = tuple(
        _material(str(index), f"shared fuse type {index}") for index in range(6)
    )

    first = rank_alternatives(materials)
    second = rank_alternatives(tuple(reversed(materials)))
    first_by_id = {result.part_id: result for result in first}
    second_by_id = {result.part_id: result for result in second}

    assert first_by_id == second_by_id
    rendered = json.dumps([asdict(result) for result in first], sort_keys=True)
    payload = json.loads(rendered)
    assert payload[0]["part_id"] == "0"
    assert payload[0]["status"] == "ok"
    assert len(payload[0]["alternatives"]) == TOP_K


def test_channel_propagates_non_vocabulary_vectorizer_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vectorizer = TfidfVectorizer()

    def fail(_descriptions: tuple[str, ...]) -> None:
        raise ValueError("unexpected vectorizer failure")

    monkeypatch.setattr(vectorizer, "fit_transform", fail)

    with pytest.raises(ValueError, match="unexpected vectorizer failure"):
        retrieval._fit_channel(("fuse",), vectorizer)


def test_structured_fallback_honors_existing_candidate_exclusions() -> None:
    materials = tuple(_material(part_id, "") for part_id in "QAB")
    normalized = tuple(
        dict.fromkeys(retrieval._FALLBACK_COLUMNS, "") for _ in materials
    )

    alternatives = retrieval._rank_structured_fallback(
        query_index=0,
        materials=materials,
        normalized=normalized,
        excluded={"A"},
        limit=5,
    )

    assert [item.part_id for item in alternatives] == ["B"]
