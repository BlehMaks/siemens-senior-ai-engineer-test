"""Deterministic lexical retrieval for alternative materials."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

# scikit-learn does not publish ``py.typed`` metadata; these two imports are the
# complete untyped boundary and results are converted to typed Python containers.
from sklearn.feature_extraction.text import (  # type: ignore[import-untyped]
    TfidfVectorizer,
)
from sklearn.metrics.pairwise import cosine_similarity  # type: ignore[import-untyped]

from material_similarity.data import (
    DESCRIPTION_COLUMN,
    PART_ID_COLUMN,
    profile_materials,
)
from material_similarity.normalize import normalize_description

TOP_K = 5
WORD_WEIGHT = 0.5
CHARACTER_WEIGHT = 0.5
_EVIDENCE_LIMIT = 5

RetrievalStatus = Literal["ok", "insufficient_description", "insufficient_candidates"]
ScoreMatrix = list[list[float]]


@dataclass(frozen=True)
class Alternative:
    """One text-supported alternative and its inspectable channel evidence."""

    part_id: str
    score: float
    word_score: float
    character_score: float
    shared_tokens: tuple[str, ...]
    shared_character_ngrams: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalResult:
    """The complete text-retrieval outcome for one catalog material."""

    part_id: str
    status: RetrievalStatus
    alternatives: tuple[Alternative, ...]


def rank_alternatives(
    materials: Sequence[Mapping[str, str]],
) -> tuple[RetrievalResult, ...]:
    """Return one deterministic, self-excluding top-five result per material."""

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
        )
        status: RetrievalStatus = (
            "ok" if len(alternatives) == TOP_K else ("insufficient_candidates")
        )
        results.append(RetrievalResult(part_id, status, alternatives))
    return tuple(results)


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
) -> tuple[Alternative, ...]:
    candidates: list[tuple[float, str, int]] = []
    for candidate_position, material_index in enumerate(usable_indices):
        if candidate_position == query_position:
            continue
        score = (
            WORD_WEIGHT * word.scores[query_position][candidate_position]
            + CHARACTER_WEIGHT * character.scores[query_position][candidate_position]
        )
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


def _shared_features(
    channel: _Channel, query_position: int, candidate_position: int
) -> tuple[str, ...]:
    shared = channel.features[query_position] & channel.features[candidate_position]
    return tuple(
        sorted(shared, key=lambda feature: (-channel.idf.get(feature, 0.0), feature))[
            :_EVIDENCE_LIMIT
        ]
    )
