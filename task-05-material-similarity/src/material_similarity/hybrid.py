"""Explicit, missing-aware structured reranking for reviewed fuse attributes."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import exp, isfinite, log
from typing import Literal

from material_similarity.data import PART_ID_COLUMN, profile_materials
from material_similarity.retrieval import (
    Alternative,
    RetrievalStatus,
    rank_alternatives,
)

_MAX_SOURCE_LENGTH = 128
_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
_RANGE = re.compile(
    rf"^\s*(\[|\()?\s*({_NUMBER})\s*(?:-|to|,)\s*({_NUMBER})\s*(\]|\))?\s*([^\d\s].*)?$",
    re.IGNORECASE,
)
_SCALAR = re.compile(rf"^\s*({_NUMBER})\s*([^\d\s].*)?$", re.IGNORECASE)
_ANNOTATION = re.compile(
    r"(?i)(?:@\([^)]{1,24}\)|\(\s*(?:typ(?:ical)?|min(?:imum)?|max(?:imum)?|ac|dc)\s*\))"
)
_SPACE = re.compile(r"\s+")

AttributeState = Literal["parsed", "missing", "unsupported", "conflict"]
Qualifier = Literal["exact", "typical", "minimum", "maximum"]
Mode = Literal["ac", "dc", "unspecified"]
HybridMode = Literal["hybrid", "text_only"]
ValueKind = Literal["current", "voltage", "length", "category"]


@dataclass(frozen=True, slots=True)
class Quantity:
    """One scalar or bounded interval in a canonical unit."""

    original: str
    lower: float
    upper: float
    lower_inclusive: bool
    upper_inclusive: bool
    unit: str
    qualifier: Qualifier
    mode: Mode = "unspecified"


@dataclass(frozen=True, slots=True)
class Dimensions:
    """Ordered package axes expressed in millimetres."""

    original: str
    axes_mm: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Category:
    """One reviewed categorical value; unknown spellings never fuzzy-match."""

    original: str
    value: str


type StructuredValue = Quantity | Dimensions | Category


@dataclass(frozen=True, slots=True)
class ParsedAttribute:
    """A logical attribute assembled from one or more source columns."""

    name: str
    state: AttributeState
    value: StructuredValue | None
    sources: tuple[str, ...]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    """One comparable field and its bounded contribution."""

    field: str
    query_value: StructuredValue
    candidate_value: StructuredValue
    similarity: float
    weight: float


@dataclass(frozen=True, slots=True)
class ScorePenalty:
    """A visible soft deduction that is not a hard incompatibility."""

    field: str
    code: str
    value: float


@dataclass(frozen=True, slots=True)
class StructuredConflict:
    """An explicit incompatibility or internally contradictory source value."""

    field: str
    code: str
    hard: bool
    query_value: StructuredValue | None
    candidate_value: StructuredValue | None


@dataclass(frozen=True, slots=True)
class UnsupportedField:
    """A field excluded from scoring with the side and parse reason preserved."""

    field: str
    side: Literal["query", "candidate"]
    reason: str


@dataclass(frozen=True, slots=True)
class HybridAlternative:
    """A text candidate with separately inspectable structured evidence."""

    part_id: str
    mode: HybridMode
    text: Alternative
    structured_score: float | None
    structured_coverage: float
    components: tuple[ScoreComponent, ...]
    penalties: tuple[ScorePenalty, ...]
    conflicts: tuple[StructuredConflict, ...]
    unsupported: tuple[UnsupportedField, ...]
    final_score: float


@dataclass(frozen=True, slots=True)
class ExcludedAlternative:
    """A generated text candidate removed only by a proven hard conflict."""

    part_id: str
    text_score: float
    conflicts: tuple[StructuredConflict, ...]


@dataclass(frozen=True, slots=True)
class HybridRetrievalResult:
    """One explicit hybrid result; the lexical result contract remains unchanged."""

    part_id: str
    status: RetrievalStatus
    alternatives: tuple[HybridAlternative, ...]
    excluded: tuple[ExcludedAlternative, ...]


@dataclass(frozen=True, slots=True)
class _AttributeSpec:
    name: str
    columns: tuple[str, ...]
    weight: float
    parser: Callable[[str], StructuredValue]
    hard_ratio: float | None = None
    hard_category: bool = False


def parse_quantity(
    raw: str,
    *,
    kind: Literal["current", "voltage", "length"],
    default_unit: str | None = None,
    mode_hint: Mode = "unspecified",
) -> Quantity:
    """Parse only reviewed electrical and distance units into a closed interval."""

    source = _bounded_source(raw)
    normalized = (
        source.replace("µ", "u")
        .replace("μ", "u")
        .replace("\N{EN DASH}", "-")
        .replace("\N{EM DASH}", "-")
    )
    lowered = normalized.casefold()
    qualifier: Qualifier = "exact"
    if "typ" in lowered:
        qualifier = "typical"
    elif "max" in lowered or normalized.lstrip().startswith(("<", "≤")):
        qualifier = "maximum"
    elif "min" in lowered or normalized.lstrip().startswith((">", "≥")):
        qualifier = "minimum"
    detected_mode: Mode = "unspecified"
    if re.search(r"\b(?:vac|ac)\b", lowered) or lowered.endswith("vac"):
        detected_mode = "ac"
    if re.search(r"\b(?:vdc|dc)\b", lowered) or lowered.endswith("vdc"):
        if detected_mode == "ac":
            raise ValueError("quantity declares both AC and DC modes")
        detected_mode = "dc"
    mode = detected_mode if detected_mode != "unspecified" else mode_hint

    cleaned = _ANNOTATION.sub("", normalized)
    cleaned = re.sub(r"(?i)\b(?:typ(?:ical)?|minimum|min|maximum|max)\b", "", cleaned)
    cleaned = cleaned.lstrip("<>≤≥ ")
    cleaned = _SPACE.sub(" ", cleaned).strip()
    match = _RANGE.fullmatch(cleaned)
    if match:
        opening = match.group(1)
        closing = match.group(4)
        if (opening is None) != (closing is None):
            raise ValueError("quantity range has unsupported delimiters")
        lower = float(match.group(2))
        upper = float(match.group(3))
        if lower > upper:
            raise ValueError("quantity range is descending")
        unit_text = (match.group(5) or default_unit or "").strip()
        lower_inclusive = opening != "("
        upper_inclusive = closing != ")"
        if lower == upper and not (lower_inclusive and upper_inclusive):
            raise ValueError("quantity range is empty")
    else:
        scalar = _SCALAR.fullmatch(cleaned)
        if not scalar:
            raise ValueError("quantity has unsupported syntax")
        lower = upper = float(scalar.group(1))
        unit_text = (scalar.group(2) or default_unit or "").strip()
        lower_inclusive = upper_inclusive = True

    factor, canonical_unit, unit_mode = _unit(kind, unit_text)
    if unit_mode != "unspecified":
        if detected_mode != "unspecified" and detected_mode != unit_mode:
            raise ValueError("quantity mode conflicts with its unit")
        mode = unit_mode
    lower *= factor
    upper *= factor
    if not all(isfinite(value) and 0.0 <= value <= 1e12 for value in (lower, upper)):
        raise ValueError("quantity magnitude is outside the supported range")
    return Quantity(
        original=source,
        lower=lower,
        upper=upper,
        lower_inclusive=lower_inclusive,
        upper_inclusive=upper_inclusive,
        unit=canonical_unit,
        qualifier=qualifier,
        mode=mode,
    )


def parse_dimensions(raw: str) -> Dimensions:
    """Parse two or three ordered package axes without inventing package-code units."""

    source = _bounded_source(raw)
    normalized = source.replace("\N{MULTIPLICATION SIGN}", "x").replace("X", "x")
    parts = tuple(part.strip() for part in normalized.split("x"))
    if len(parts) not in (2, 3) or any(not part for part in parts):
        raise ValueError("dimensions require two or three ordered axes")
    trailing_unit_match = re.search(r"(?i)(mm|cm|m)\s*$", parts[-1])
    trailing_unit = trailing_unit_match.group(1) if trailing_unit_match else None
    axes: list[float] = []
    for part in parts:
        quantity = parse_quantity(
            part,
            kind="length",
            default_unit=trailing_unit,
        )
        if quantity.lower != quantity.upper:
            raise ValueError("dimension axes must be scalar")
        axes.append(quantity.lower)
    return Dimensions(source, tuple(axes))


def parse_category(raw: str, *, aliases: Mapping[str, str]) -> Category:
    """Map only explicitly reviewed aliases; no fuzzy matching is allowed."""

    source = _bounded_source(raw)
    key = _category_key(source)
    try:
        value = aliases[key]
    except KeyError:
        raise ValueError("category spelling is unsupported") from None
    return Category(source, value)


def parse_material_attributes(
    material: Mapping[str, str],
) -> tuple[ParsedAttribute, ...]:
    """Parse the fixed high-value subset and expose missing/unsupported/conflict states."""

    return tuple(_parse_attribute(material, spec) for spec in _SPECS)


def rank_hybrid_alternatives(
    materials: Sequence[Mapping[str, str]],
    *,
    text_weight: float = 0.65,
    minimum_structured_coverage: float = 0.25,
) -> tuple[HybridRetrievalResult, ...]:
    """Rerank text-generated top-five candidates with explicit structured evidence."""

    if not isfinite(text_weight) or not 0.0 <= text_weight <= 1.0:
        raise ValueError("text_weight must be finite and between zero and one")
    if (
        not isfinite(minimum_structured_coverage)
        or not 0.0 <= minimum_structured_coverage <= 1.0
    ):
        raise ValueError("minimum structured coverage must be between zero and one")
    profile_materials(materials)
    by_id = {material[PART_ID_COLUMN].strip(): material for material in materials}
    parsed = {
        part_id: {item.name: item for item in parse_material_attributes(material)}
        for part_id, material in by_id.items()
    }
    text_results = rank_alternatives(materials)
    results: list[HybridRetrievalResult] = []
    for text_result in text_results:
        alternatives: list[HybridAlternative] = []
        excluded: list[ExcludedAlternative] = []
        for text_alternative in text_result.alternatives:
            scored = _score_candidate(
                text_alternative,
                parsed[text_result.part_id],
                parsed[text_alternative.part_id],
                text_weight=text_weight,
                minimum_coverage=minimum_structured_coverage,
            )
            hard_conflicts = tuple(
                conflict for conflict in scored.conflicts if conflict.hard
            )
            if hard_conflicts:
                excluded.append(
                    ExcludedAlternative(
                        part_id=scored.part_id,
                        text_score=scored.text.score,
                        conflicts=hard_conflicts,
                    )
                )
            else:
                alternatives.append(scored)
        alternatives.sort(key=lambda item: (-item.final_score, item.part_id))
        status: RetrievalStatus
        if text_result.status == "insufficient_description":
            status = "insufficient_description"
        elif len(alternatives) == 5:
            status = "ok"
        else:
            status = "insufficient_candidates"
        results.append(
            HybridRetrievalResult(
                part_id=text_result.part_id,
                status=status,
                alternatives=tuple(alternatives),
                excluded=tuple(excluded),
            )
        )
    return tuple(results)


def _score_candidate(
    text: Alternative,
    query: Mapping[str, ParsedAttribute],
    candidate: Mapping[str, ParsedAttribute],
    *,
    text_weight: float,
    minimum_coverage: float,
) -> HybridAlternative:
    components: list[ScoreComponent] = []
    penalties: list[ScorePenalty] = []
    conflicts: list[StructuredConflict] = []
    unsupported: list[UnsupportedField] = []
    comparable_weight = 0.0
    weighted_score = 0.0
    for spec in _SPECS:
        left = query[spec.name]
        right = candidate[spec.name]
        if left.state == "unsupported":
            unsupported.append(
                UnsupportedField(spec.name, "query", left.reason or "unsupported")
            )
        if right.state == "unsupported":
            unsupported.append(
                UnsupportedField(spec.name, "candidate", right.reason or "unsupported")
            )
        if left.state == "conflict" or right.state == "conflict":
            conflicts.append(
                StructuredConflict(
                    spec.name,
                    "contradictory_source_fields",
                    True,
                    left.value,
                    right.value,
                )
            )
            continue
        if left.state != "parsed" or right.state != "parsed":
            continue
        assert left.value is not None and right.value is not None
        similarity, conflict = _compare(spec, left.value, right.value)
        comparable_weight += spec.weight
        weighted_score += spec.weight * similarity
        components.append(
            ScoreComponent(
                spec.name,
                left.value,
                right.value,
                round(similarity, 6),
                spec.weight,
            )
        )
        if conflict is not None:
            conflicts.append(conflict)
        elif similarity < 1.0 and isinstance(left.value, Category):
            penalty = round(0.05 * spec.weight / _TOTAL_WEIGHT, 6)
            penalties.append(ScorePenalty(spec.name, "categorical_mismatch", penalty))
        if (
            isinstance(left.value, Quantity)
            and isinstance(right.value, Quantity)
            and left.value.qualifier != right.value.qualifier
        ):
            penalty = round(0.02 * spec.weight / _TOTAL_WEIGHT, 6)
            penalties.append(ScorePenalty(spec.name, "qualifier_mismatch", penalty))

    coverage = comparable_weight / _TOTAL_WEIGHT
    structured = weighted_score / comparable_weight if comparable_weight else None
    penalty_total = sum(item.value for item in penalties)
    if structured is None or coverage < minimum_coverage:
        mode: HybridMode = "text_only"
        final = text.score
    else:
        mode = "hybrid"
        final = text_weight * text.score + (1.0 - text_weight) * structured
        final = max(0.0, min(1.0, final - penalty_total))
    return HybridAlternative(
        part_id=text.part_id,
        mode=mode,
        text=text,
        structured_score=None if structured is None else round(structured, 6),
        structured_coverage=round(coverage, 6),
        components=tuple(components),
        penalties=tuple(penalties),
        conflicts=tuple(conflicts),
        unsupported=tuple(unsupported),
        final_score=round(final, 6),
    )


def _parse_attribute(
    material: Mapping[str, str], spec: _AttributeSpec
) -> ParsedAttribute:
    observed = tuple(
        (column, material[column].strip())
        for column in spec.columns
        if material[column].strip()
    )
    if not observed:
        return ParsedAttribute(spec.name, "missing", None, ())
    values: list[StructuredValue] = []
    sources: list[str] = []
    for column, raw in observed:
        sources.append(column)
        try:
            values.append(spec.parser(raw))
        except ValueError as exc:
            return ParsedAttribute(
                spec.name,
                "unsupported",
                None,
                tuple(sources),
                str(exc),
            )
    first = values[0]
    if any(not _equivalent(first, value) for value in values[1:]):
        return ParsedAttribute(
            spec.name,
            "conflict",
            None,
            tuple(sources),
            "source fields disagree",
        )
    return ParsedAttribute(spec.name, "parsed", first, tuple(sources))


def _compare(
    spec: _AttributeSpec,
    left: StructuredValue,
    right: StructuredValue,
) -> tuple[float, StructuredConflict | None]:
    if type(left) is not type(right):
        return 0.0, StructuredConflict(
            spec.name, "value_kind_mismatch", True, left, right
        )
    if isinstance(left, Category) and isinstance(right, Category):
        if left.value == right.value:
            return 1.0, None
        conflict = StructuredConflict(
            spec.name,
            "categorical_mismatch",
            spec.hard_category,
            left,
            right,
        )
        return 0.0, conflict
    if isinstance(left, Dimensions) and isinstance(right, Dimensions):
        if len(left.axes_mm) != len(right.axes_mm):
            return 0.0, StructuredConflict(
                spec.name, "dimension_axis_mismatch", True, left, right
            )
        similarities = tuple(
            _positive_similarity(a, b)
            for a, b in zip(left.axes_mm, right.axes_mm, strict=True)
        )
        hard = any(
            max(a, b) / min(a, b) >= 1.5
            for a, b in zip(left.axes_mm, right.axes_mm, strict=True)
            if min(a, b) > 0
        )
        dimension_conflict = (
            StructuredConflict(spec.name, "dimension_mismatch", True, left, right)
            if hard
            else None
        )
        return sum(similarities) / len(similarities), dimension_conflict
    if not isinstance(left, Quantity) or not isinstance(right, Quantity):
        raise TypeError("unsupported structured comparison")
    if left.unit != right.unit or (
        left.mode != "unspecified"
        and right.mode != "unspecified"
        and left.mode != right.mode
    ):
        return 0.0, StructuredConflict(
            spec.name, "unit_or_mode_mismatch", True, left, right
        )
    similarity = _interval_similarity(left, right)
    if spec.hard_ratio is not None and not _intervals_overlap(left, right):
        left_mid = (left.lower + left.upper) / 2.0
        right_mid = (right.lower + right.upper) / 2.0
        if min(left_mid, right_mid) == 0.0:
            hard = max(left_mid, right_mid) > 0.0
        else:
            hard = (
                max(left_mid, right_mid) / min(left_mid, right_mid) >= spec.hard_ratio
            )
        if hard:
            return similarity, StructuredConflict(
                spec.name, "numeric_hard_conflict", True, left, right
            )
    return similarity, None


def _interval_similarity(left: Quantity, right: Quantity) -> float:
    overlaps = _intervals_overlap(left, right)
    endpoint = (
        _positive_similarity(left.lower, right.lower)
        + _positive_similarity(left.upper, right.upper)
    ) / 2.0
    return min(1.0, 0.5 + 0.5 * endpoint) if overlaps else endpoint


def _intervals_overlap(left: Quantity, right: Quantity) -> bool:
    overlap_lower = max(left.lower, right.lower)
    overlap_upper = min(left.upper, right.upper)
    return overlap_lower < overlap_upper or (
        overlap_lower == overlap_upper
        and (
            left.lower_inclusive
            if overlap_lower == left.lower
            else left.upper_inclusive
        )
        and (
            right.lower_inclusive
            if overlap_lower == right.lower
            else right.upper_inclusive
        )
    )


def _positive_similarity(left: float, right: float) -> float:
    if left == right:
        return 1.0
    if left <= 0.0 or right <= 0.0:
        return 0.0
    return exp(-abs(log(left / right)))


def _equivalent(left: StructuredValue, right: StructuredValue) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, Quantity) and isinstance(right, Quantity):
        return (
            left.lower,
            left.upper,
            left.lower_inclusive,
            left.upper_inclusive,
            left.unit,
            left.qualifier,
            left.mode,
        ) == (
            right.lower,
            right.upper,
            right.lower_inclusive,
            right.upper_inclusive,
            right.unit,
            right.qualifier,
            right.mode,
        )
    if isinstance(left, Dimensions) and isinstance(right, Dimensions):
        return left.axes_mm == right.axes_mm
    if isinstance(left, Category) and isinstance(right, Category):
        return left.value == right.value
    return False


def _unit(
    kind: Literal["current", "voltage", "length"], raw_unit: str
) -> tuple[float, str, Mode]:
    source_unit = re.sub(r"\s+", "", raw_unit)
    if kind in {"current", "voltage"} and source_unit.startswith("M"):
        raise ValueError(f"unsupported {kind} unit")
    unit = source_unit.casefold()
    tables: dict[str, dict[str, tuple[float, str, Mode]]] = {
        "current": {
            "a": (1.0, "A", "unspecified"),
            "amp": (1.0, "A", "unspecified"),
            "amps": (1.0, "A", "unspecified"),
            "ma": (1e-3, "A", "unspecified"),
            "ua": (1e-6, "A", "unspecified"),
        },
        "voltage": {
            "v": (1.0, "V", "unspecified"),
            "vac": (1.0, "V", "ac"),
            "vdc": (1.0, "V", "dc"),
            "mv": (1e-3, "V", "unspecified"),
            "mvac": (1e-3, "V", "ac"),
            "mvdc": (1e-3, "V", "dc"),
            "kv": (1e3, "V", "unspecified"),
            "kvac": (1e3, "V", "ac"),
            "kvdc": (1e3, "V", "dc"),
        },
        "length": {
            "mm": (1.0, "mm", "unspecified"),
            "cm": (10.0, "mm", "unspecified"),
            "m": (1_000.0, "mm", "unspecified"),
        },
    }
    try:
        return tables[kind][unit]
    except KeyError:
        raise ValueError(f"unsupported {kind} unit") from None


def _bounded_source(raw: str) -> str:
    if type(raw) is not str:
        raise ValueError("source value must be text")
    source = raw.strip()
    if not source:
        raise ValueError("source value is blank")
    if len(source) > _MAX_SOURCE_LENGTH or any(
        ord(character) < 32 for character in source
    ):
        raise ValueError("source value is malformed or too long")
    return source


def _category_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", raw.casefold()).strip()


_ACTING_ALIASES = {
    "fast": "fast",
    "fast acting": "fast",
    "quick acting": "fast",
    "slow": "slow",
    "slow blow": "slow",
    "time delay": "slow",
    "very fast": "very_fast",
    "very fast acting": "very_fast",
}
_MATERIAL_ALIASES = {
    "ceramic": "ceramic",
    "glass": "glass",
    "metal": "metal",
}
_MOUNTING_ALIASES = {
    "surface mount": "surface",
    "surface mounting": "surface",
    "smd": "surface",
    "through hole": "through_hole",
    "throughhole": "through_hole",
    "inline": "inline",
    "holder": "holder",
}
_MOUNTING_FEATURE_ALIASES = {
    "yes": "true",
    "true": "true",
    "with holder": "true",
    "no": "false",
    "false": "false",
    "without holder": "false",
}


def _quantity_parser(
    kind: Literal["current", "voltage", "length"],
    *,
    default_unit: str,
    mode_hint: Mode = "unspecified",
) -> Callable[[str], Quantity]:
    return lambda raw: parse_quantity(
        raw,
        kind=kind,
        default_unit=default_unit,
        mode_hint=mode_hint,
    )


def _category_parser(aliases: Mapping[str, str]) -> Callable[[str], Category]:
    return lambda raw: parse_category(raw, aliases=aliases)


_SPECS = (
    _AttributeSpec(
        "current",
        ("Current Rating", "Rated Current (A)"),
        3.0,
        _quantity_parser("current", default_unit="A"),
        hard_ratio=4.0,
    ),
    _AttributeSpec(
        "ac_voltage",
        ("Maximum AC Voltage Rating", "Rated Voltage(AC) (V)"),
        2.0,
        _quantity_parser("voltage", default_unit="V", mode_hint="ac"),
        hard_ratio=2.0,
    ),
    _AttributeSpec(
        "dc_voltage",
        ("Maximum DC Voltage Rating", "Rated Voltage(DC) (V)"),
        2.0,
        _quantity_parser("voltage", default_unit="V", mode_hint="dc"),
        hard_ratio=2.0,
    ),
    _AttributeSpec(
        "dimensions",
        ("Fuse Size", "Physical Dimension"),
        3.0,
        parse_dimensions,
    ),
    _AttributeSpec(
        "acting",
        ("Acting", "Blow Characteristic"),
        3.0,
        _category_parser(_ACTING_ALIASES),
        hard_category=True,
    ),
    _AttributeSpec(
        "material",
        ("Fuse Material",),
        1.0,
        _category_parser(_MATERIAL_ALIASES),
    ),
    _AttributeSpec(
        "mounting",
        ("Mounting",),
        1.0,
        _category_parser(_MOUNTING_ALIASES),
    ),
    _AttributeSpec(
        "mounting_feature",
        ("Mounting Feature",),
        0.5,
        _category_parser(_MOUNTING_FEATURE_ALIASES),
    ),
)
_TOTAL_WEIGHT = sum(spec.weight for spec in _SPECS)


__all__ = [
    "Category",
    "Dimensions",
    "ExcludedAlternative",
    "HybridAlternative",
    "HybridRetrievalResult",
    "ParsedAttribute",
    "Quantity",
    "ScoreComponent",
    "ScorePenalty",
    "StructuredConflict",
    "UnsupportedField",
    "parse_category",
    "parse_dimensions",
    "parse_material_attributes",
    "parse_quantity",
    "rank_hybrid_alternatives",
]
