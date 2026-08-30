"""Deterministic lexical retrieval for alternative materials."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Literal, cast

# scikit-learn does not publish ``py.typed`` metadata; these two imports are the
# complete untyped boundary and results are converted to typed Python containers.
from sklearn.feature_extraction.text import (  # type: ignore[import-untyped]
    TfidfVectorizer,
)
from sklearn.metrics.pairwise import cosine_similarity  # type: ignore[import-untyped]

from material_similarity.data import (
    DESCRIPTION_COLUMN,
    MATERIAL_COLUMNS,
    PART_ID_COLUMN,
    profile_materials,
)
from material_similarity.normalize import normalize_description

TOP_K = 5
WORD_WEIGHT = 0.25
_EVIDENCE_LIMIT = 5

RetrievalStatus = Literal["ok", "insufficient_description", "insufficient_candidates"]
RetrievalMethod = Literal["description", "structured_fallback"]
RetrievalConfidence = Literal[
    "description_supported", "field_supported", "missingness_only"
]
ScoreMatrix = list[list[float]]
_FALLBACK_COLUMNS = tuple(
    column
    for column in MATERIAL_COLUMNS
    if column not in {PART_ID_COLUMN, DESCRIPTION_COLUMN}
)


@dataclass(frozen=True)
class Alternative:
    """One text-supported alternative and its inspectable channel evidence."""

    part_id: str
    score: float
    word_score: float
    character_score: float
    shared_tokens: tuple[str, ...]
    shared_character_ngrams: tuple[str, ...]
    method: RetrievalMethod = "description"
    confidence: RetrievalConfidence = "description_supported"
    shared_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalResult:
    """The complete text-retrieval outcome for one catalog material."""

    part_id: str
    status: RetrievalStatus
    alternatives: tuple[Alternative, ...]


def rank_alternatives(
    materials: Sequence[Mapping[str, str]],
    *,
    word_weight: float = WORD_WEIGHT,
) -> tuple[RetrievalResult, ...]:
    """Return one deterministic, self-excluding top-five result per material."""

    if not isfinite(word_weight) or not 0.0 <= word_weight <= 1.0:
        raise ValueError("word_weight must be finite and between 0 and 1")

    # Reuse the catalog boundary so direct callers cannot bypass unique-ID and
    # schema invariants already enforced by the loader.
    profile_materials(materials)
    part_ids = tuple(material[PART_ID_COLUMN].strip() for material in materials)
    descriptions = tuple(
        normalize_description(material[DESCRIPTION_COLUMN]) for material in materials
    )
    usable_indices = tuple(
        index for index, description in enumerate(descriptions) if description
    )
    usable_descriptions = tuple(descriptions[index] for index in usable_indices)

    word = _fit_channel(
        usable_descriptions,
        TfidfVectorizer(
            lowercase=False,
            ngram_range=(1, 2),
            token_pattern=r"(?u)\b[\w./@-]+\b",
        ),
    )
    character = _fit_channel(
        usable_descriptions,
        TfidfVectorizer(
            analyzer="char_wb",
            lowercase=False,
            ngram_range=(3, 5),
        ),
    )
    usable_position = {
        material_index: position
        for position, material_index in enumerate(usable_indices)
    }

    results: list[RetrievalResult] = []
    for material_index, part_id in enumerate(part_ids):
        query_position = usable_position.get(material_index)
        if query_position is None:
            results.append(RetrievalResult(part_id, "insufficient_description", ()))
            continue

        alternatives = _rank_query(
            query_position=query_position,
            usable_indices=usable_indices,
            part_ids=part_ids,
            word=word,
            character=character,
            word_weight=word_weight,
        )
        status: RetrievalStatus = (
            "ok" if len(alternatives) == TOP_K else ("insufficient_candidates")
        )
        results.append(RetrievalResult(part_id, status, alternatives))
    return tuple(results)


def rank_complete_alternatives(
    materials: Sequence[Mapping[str, str]],
    *,
    word_weight: float = WORD_WEIGHT,
) -> tuple[RetrievalResult, ...]:
    """Return five labeled alternatives per row, filling text gaps structurally."""

    text_results = rank_alternatives(materials, word_weight=word_weight)
    normalized = tuple(
        {
            column: normalize_description(material[column])
            for column in _FALLBACK_COLUMNS
        }
        for material in materials
    )
    completed: list[RetrievalResult] = []
    for query_index, text_result in enumerate(text_results):
        if len(text_result.alternatives) == TOP_K:
            completed.append(text_result)
            continue
        existing = {item.part_id for item in text_result.alternatives}
        fallback = _rank_structured_fallback(
            query_index=query_index,
            materials=materials,
            normalized=normalized,
            excluded=existing,
            limit=TOP_K - len(text_result.alternatives),
        )
        alternatives = (*text_result.alternatives, *fallback)
        status: RetrievalStatus = (
            "ok" if len(alternatives) == TOP_K else "insufficient_candidates"
        )
        completed.append(
            RetrievalResult(text_result.part_id, status, tuple(alternatives))
        )
    return tuple(completed)


@dataclass(frozen=True)
class _Channel:
    scores: ScoreMatrix
    features: tuple[frozenset[str], ...]
    idf: dict[str, float]


def _fit_channel(
    descriptions: tuple[str, ...], vectorizer: TfidfVectorizer
) -> _Channel:
    analyzer = vectorizer.build_analyzer()
    features = tuple(frozenset(analyzer(description)) for description in descriptions)
    try:
        matrix = vectorizer.fit_transform(descriptions)
    except ValueError as error:
        if "empty vocabulary" not in str(error):
            raise
        # Non-blank punctuation-only descriptions are not evidence. Keeping a zero
        # channel lets the other channel work and prevents fabricated neighbours.
        size = len(descriptions)
        return _Channel([[0.0] * size for _ in descriptions], features, {})

    scores = cast(ScoreMatrix, cosine_similarity(matrix).tolist())
    names = cast(list[str], vectorizer.get_feature_names_out().tolist())
    idf_values = cast(list[float], vectorizer.idf_.tolist())
    return _Channel(scores, features, dict(zip(names, idf_values, strict=True)))


def _rank_query(
    *,
    query_position: int,
    usable_indices: tuple[int, ...],
    part_ids: tuple[str, ...],
    word: _Channel,
    character: _Channel,
    word_weight: float,
) -> tuple[Alternative, ...]:
    candidates: list[tuple[float, str, int]] = []
    for candidate_position, material_index in enumerate(usable_indices):
        if candidate_position == query_position:
            continue
        score = (
            word_weight * word.scores[query_position][candidate_position]
            + (1.0 - word_weight) * character.scores[query_position][candidate_position]
        )
        # Quantize machine noise so mathematically tied candidates always reach the
        # documented PART_ID tie-break regardless of source row order.
        score = round(score, 12)
        # Zero overlap is not text evidence, so it must not be padded into a top five.
        if score > 0.0:
            candidates.append((score, part_ids[material_index], candidate_position))

    candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
    return tuple(
        _alternative(
            part_id=part_id,
            score=score,
            query_position=query_position,
            candidate_position=candidate_position,
            word=word,
            character=character,
        )
        for score, part_id, candidate_position in candidates[:TOP_K]
    )


def _alternative(
    *,
    part_id: str,
    score: float,
    query_position: int,
    candidate_position: int,
    word: _Channel,
    character: _Channel,
) -> Alternative:
    return Alternative(
        part_id=part_id,
        score=round(score, 6),
        word_score=round(word.scores[query_position][candidate_position], 6),
        character_score=round(character.scores[query_position][candidate_position], 6),
        shared_tokens=_shared_features(word, query_position, candidate_position),
        shared_character_ngrams=_shared_features(
            character, query_position, candidate_position
        ),
    )


def _rank_structured_fallback(
    *,
    query_index: int,
    materials: Sequence[Mapping[str, str]],
    normalized: tuple[dict[str, str], ...],
    excluded: set[str],
    limit: int,
) -> tuple[Alternative, ...]:
    query = normalized[query_index]
    observed = tuple(column for column in _FALLBACK_COLUMNS if query[column])
    missing = tuple(column for column in _FALLBACK_COLUMNS if not query[column])
    candidates: list[tuple[float, str, RetrievalConfidence, tuple[str, ...]]] = []
    for candidate_index, candidate in enumerate(materials):
        if candidate_index == query_index:
            continue
        part_id = candidate[PART_ID_COLUMN].strip()
        if part_id in excluded:
            continue
        values = normalized[candidate_index]
        exact = tuple(
            column
            for column in observed
            if values[column] and values[column] == query[column]
        )
        matching_missingness = tuple(column for column in missing if not values[column])
        score = _structured_fallback_score(
            exact_matches=len(exact),
            observed_fields=len(observed),
            matching_missingness=len(matching_missingness),
            missing_fields=len(missing),
        )
        if exact:
            confidence: RetrievalConfidence = "field_supported"
            evidence = exact
        else:
            confidence = "missingness_only"
            evidence = tuple(f"missing:{column}" for column in matching_missingness)
        candidates.append((score, part_id, confidence, evidence[:_EVIDENCE_LIMIT]))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return tuple(
        Alternative(
            part_id=part_id,
            score=round(score, 6),
            word_score=0.0,
            character_score=0.0,
            shared_tokens=(),
            shared_character_ngrams=(),
            method="structured_fallback",
            confidence=confidence,
            shared_fields=evidence,
        )
        for score, part_id, confidence, evidence in candidates[:limit]
    )


def _structured_fallback_score(
    *,
    exact_matches: int,
    observed_fields: int,
    matching_missingness: int,
    missing_fields: int,
) -> float:
    if observed_fields == 0:
        return matching_missingness / missing_fields
    exact_score = exact_matches / observed_fields
    missingness_score = matching_missingness / missing_fields if missing_fields else 1.0
    return round(0.9 * exact_score + 0.1 * missingness_score, 12)


def _shared_features(
    channel: _Channel, query_position: int, candidate_position: int
) -> tuple[str, ...]:
    shared = channel.features[query_position] & channel.features[candidate_position]
    return tuple(
        sorted(shared, key=lambda feature: (-channel.idf.get(feature, 0.0), feature))[
            :_EVIDENCE_LIMIT
        ]
    )
