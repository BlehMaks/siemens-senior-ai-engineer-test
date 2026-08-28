from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from material_similarity.data import (
    DESCRIPTION_COLUMN,
    MATERIAL_COLUMNS,
    PART_ID_COLUMN,
)
from material_similarity.evaluation import (
    Benchmark,
    BenchmarkQuery,
    Judgment,
    evaluate_hybrid_benchmark,
)
from material_similarity.hybrid import (
    Category,
    Dimensions,
    Quantity,
    parse_category,
    parse_dimensions,
    parse_material_attributes,
    parse_quantity,
    rank_hybrid_alternatives,
)


def _material(
    part_id: str,
    description: str = "slow glass cartridge fuse",
    **values: str,
) -> dict[str, str]:
    material = dict.fromkeys(MATERIAL_COLUMNS, "")
    material[PART_ID_COLUMN] = part_id
    material[DESCRIPTION_COLUMN] = description
    material.update(values)
    return material


def _rated_material(part_id: str, **values: str) -> dict[str, str]:
    defaults = {
        "Current Rating": "2A",
        "Maximum AC Voltage Rating": "250VAC",
        "Fuse Size": "6.3mm x 32mm",
        "Acting": "slow",
        "Blow Characteristic": "time delay",
        "Fuse Material": "glass",
        "Mounting": "holder",
    }
    defaults.update(values)
    return _material(part_id, **defaults)


def test_quantity_parser_normalizes_units_ranges_qualifiers_and_modes() -> None:
    current = parse_quantity("6.3@(CSA/UL)A", kind="current")
    milliamps = parse_quantity("6300mA", kind="current")
    bounded = parse_quantity("[5, 6.3) A", kind="current")
    typical = parse_quantity("6.3(Typ)A", kind="current")
    ac = parse_quantity("250VAC", kind="voltage")
    dc = parse_quantity("0.25kVDC", kind="voltage")

    assert current.lower == current.upper == 6.3
    assert milliamps.lower == current.lower
    assert (bounded.lower, bounded.upper) == (5.0, 6.3)
    assert bounded.lower_inclusive is True
    assert bounded.upper_inclusive is False
    assert typical.qualifier == "typical"
    assert ac.mode == "ac"
    assert dc.mode == "dc"
    assert dc.lower == 250.0


def test_dimension_parser_preserves_axis_order_and_converts_to_millimetres() -> None:
    first = parse_dimensions("5.2mm \N{MULTIPLICATION SIGN} 20mm")
    second = parse_dimensions("0.52cm x 0.02m")

    assert first == Dimensions("5.2mm \N{MULTIPLICATION SIGN} 20mm", (5.2, 20.0))
    assert second.axes_mm == pytest.approx((5.2, 20.0))
    assert parse_dimensions("20mm x 5.2mm").axes_mm == (20.0, 5.2)


@pytest.mark.parametrize(
    "raw",
    (
        "",
        "not-a-value",
        "6A 7A",
        "7-6A",
        "1kg",
        "1A\n2A",
        "1" * 129,
    ),
)
def test_quantity_parser_rejects_malformed_or_unsupported_values(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_quantity(raw, kind="current")


def test_category_parser_uses_only_reviewed_aliases_and_boolean_values() -> None:
    aliases = {"yes": "true", "no": "false", "surface mount": "surface"}

    assert parse_category("YES", aliases=aliases) == Category("YES", "true")
    assert parse_category("surface-mount", aliases=aliases).value == "surface"
    with pytest.raises(ValueError, match="unsupported"):
        parse_category("probably", aliases=aliases)


def test_multiple_source_fields_expose_conflict_instead_of_column_precedence() -> None:
    material = _rated_material("Q", **{"Rated Current (A)": "5A"})

    attributes = {item.name: item for item in parse_material_attributes(material)}

    assert attributes["current"].state == "conflict"
    assert attributes["current"].value is None
    assert attributes["current"].sources == (
        "Current Rating",
        "Rated Current (A)",
    )


def test_hybrid_ranking_filters_only_hard_conflicts_and_explains_every_score() -> None:
    materials = (
        _rated_material("Q"),
        _rated_material("A"),
        _rated_material("B", Acting="fast", **{"Blow Characteristic": "fast"}),
        _rated_material("C", **{"Current Rating": "20A"}),
        _rated_material("D", **{"Current Rating": "2.5A"}),
        _material("E"),
        _rated_material("F", **{"Fuse Size": "5mm x 20mm"}),
    )

    query = next(
        result
        for result in rank_hybrid_alternatives(materials)
        if result.part_id == "Q"
    )

    assert query.status == "insufficient_candidates"
    assert {item.part_id for item in query.excluded} == {"B", "C"}
    assert {item.part_id for item in query.alternatives} == {"A", "D", "E"}
    exact = next(item for item in query.alternatives if item.part_id == "A")
    partial = next(item for item in query.alternatives if item.part_id == "D")
    missing = next(item for item in query.alternatives if item.part_id == "E")
    assert exact.mode == "hybrid"
    assert exact.structured_score == 1.0
    assert exact.final_score == 1.0
    assert {component.field for component in exact.components} >= {
        "current",
        "ac_voltage",
        "dimensions",
        "acting",
    }
    assert partial.structured_score is not None
    assert 0.0 < partial.structured_score < 1.0
    assert missing.mode == "text_only"
    assert missing.structured_score is None
    assert missing.structured_coverage == 0.0


def test_ac_dc_and_dimension_conflicts_are_hard_and_visible() -> None:
    materials = (
        _rated_material("Q"),
        _rated_material(
            "A",
            **{
                "Maximum AC Voltage Rating": "250VDC",
                "Fuse Size": "2mm x 10mm",
            },
        ),
        *(_rated_material(part_id) for part_id in "BCDEF"),
    )

    query = next(
        result
        for result in rank_hybrid_alternatives(materials)
        if result.part_id == "Q"
    )
    excluded = next(item for item in query.excluded if item.part_id == "A")

    assert {conflict.code for conflict in excluded.conflicts} >= {
        "unit_or_mode_mismatch",
        "dimension_mismatch",
    }
    assert all(conflict.hard for conflict in excluded.conflicts)


def test_hybrid_results_are_deterministic_bounded_and_json_serializable() -> None:
    materials = tuple(_rated_material(part_id) for part_id in "QABCDEFG")

    first = rank_hybrid_alternatives(materials)
    second = rank_hybrid_alternatives(tuple(reversed(materials)))
    first_by_id = {result.part_id: result for result in first}
    second_by_id = {result.part_id: result for result in second}

    assert first_by_id == second_by_id
    query = first_by_id["Q"]
    assert [item.part_id for item in query.alternatives] == ["A", "B", "C", "D", "E"]
    assert all(0.0 <= item.final_score <= 1.0 for item in query.alternatives)
    assert all(0.0 <= item.structured_coverage <= 1.0 for item in query.alternatives)
    rendered = json.dumps(asdict(query), sort_keys=True)
    assert json.loads(rendered)["part_id"] == "Q"


def test_hybrid_weight_and_coverage_controls_are_strict() -> None:
    materials = tuple(_rated_material(part_id) for part_id in "QABCDEF")

    with pytest.raises(ValueError, match="text_weight"):
        rank_hybrid_alternatives(materials, text_weight=float("nan"))
    with pytest.raises(ValueError, match="coverage"):
        rank_hybrid_alternatives(materials, minimum_structured_coverage=1.1)

    query = rank_hybrid_alternatives(
        materials,
        minimum_structured_coverage=1.0,
    )[0]
    assert all(item.mode == "text_only" for item in query.alternatives)


def test_quantity_dataclass_is_strictly_typed_for_explanations() -> None:
    parsed = parse_quantity("1-2A", kind="current")

    assert isinstance(parsed, Quantity)
    assert parsed.unit == "A"
    assert parsed.mode == "unspecified"


def test_hybrid_evaluation_requires_relevance_and_hard_negative_improvement() -> None:
    materials = (
        _rated_material("Q"),
        _rated_material("A"),
        _rated_material("B", Acting="fast", **{"Blow Characteristic": "fast"}),
        *(_rated_material(part_id) for part_id in "CDEF"),
    )
    benchmark = Benchmark(
        "fixture",
        len(materials),
        (
            BenchmarkQuery(
                part_id="Q",
                expected_status="ok",
                slices=("structured-hard-negative",),
                rationale="B matches text but conflicts on acting characteristic",
                judgments=tuple(
                    Judgment(
                        part_id,
                        0 if part_id == "B" else 2,
                        "synthetic structured comparison fixture",
                    )
                    for part_id in "ABCDE"
                ),
            ),
        ),
    )

    report = evaluate_hybrid_benchmark(materials, benchmark)

    assert report.text_hard_negative_rate == 0.2
    assert report.hybrid_hard_negative_rate == 0.0
    assert report.text_metrics.coverage == 1.0
    assert report.hybrid_metrics.coverage == 0.0
    assert report.hybrid_stability == 1.0
    assert report.promoted is False
    assert report.non_promotion_reasons == (
        "hybrid nDCG@5 did not improve",
        "hybrid coverage regressed",
        "hybrid expected-status agreement regressed",
    )
