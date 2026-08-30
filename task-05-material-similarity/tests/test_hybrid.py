from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

import pytest

import material_similarity.hybrid as hybrid_module
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
    DEFAULT_COMPATIBILITY_POLICY,
    Category,
    Dimensions,
    HybridRetrievalResult,
    Quantity,
    assess_compatibility,
    load_compatibility_policy,
    parse_category,
    parse_dimensions,
    parse_material_attributes,
    parse_quantity,
    rank_business_alternatives,
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
        "Mounting Feature": "yes",
    }
    defaults.update(values)
    return _material(part_id, **defaults)


def test_reviewed_policy_loads_without_a_yaml_runtime_dependency() -> None:
    path = Path(__file__).parents[1] / "evals" / "compatibility-policy.yaml"

    policy = load_compatibility_policy(path)

    assert policy == DEFAULT_COMPATIBILITY_POLICY
    assert policy.to_dict()["schema_version"] == "1.0"
    assert all(rule.never_relax for rule in policy.rules if rule.hard_category)


@pytest.mark.parametrize(
    ("changes", "field", "code"),
    (
        ({"Current Rating": "20A"}, "current", "numeric_hard_conflict"),
        (
            {"Maximum AC Voltage Rating": "250VDC"},
            "ac_voltage",
            "unit_or_mode_mismatch",
        ),
        (
            {"Maximum DC Voltage Rating": "125VAC"},
            "dc_voltage",
            "unit_or_mode_mismatch",
        ),
        ({"Fuse Size": "2mm x 10mm"}, "dimensions", "dimension_mismatch"),
        (
            {"Acting": "fast", "Blow Characteristic": "fast"},
            "acting",
            "categorical_mismatch",
        ),
        ({"Fuse Material": "ceramic"}, "material", "categorical_mismatch"),
        ({"Mounting": "surface mount"}, "mounting", "categorical_mismatch"),
        ({"Mounting Feature": "no"}, "mounting_feature", "categorical_mismatch"),
    ),
)
def test_strict_policy_hard_gates_each_reviewed_rule(
    changes: dict[str, str], field: str, code: str
) -> None:
    assessment = assess_compatibility(
        _rated_material(
            "Q",
            **(
                {"Maximum DC Voltage Rating": "125VDC"} if field == "dc_voltage" else {}
            ),
        ),
        _rated_material("C", **changes),
    )

    assert assessment.outcome == "conflict"
    assert any(
        conflict.field == field and conflict.code == code and conflict.hard
        for conflict in assessment.conflicts
    )


def test_strict_policy_distinguishes_missing_and_unsupported_evidence() -> None:
    missing = assess_compatibility(_rated_material("Q"), _material("M"))
    unsupported = assess_compatibility(
        _rated_material("Q"),
        _material("U", Acting="instantaneous"),
    )

    assert missing.outcome == "insufficient_evidence"
    assert missing.unsupported == ()
    assert unsupported.outcome == "conflict"
    assert [(item.field, item.side) for item in unsupported.unsupported] == [
        ("acting", "candidate")
    ]
    assert unsupported.conflicts[0].code == "unsupported_never_relaxed_rule"


def test_business_mode_never_lets_an_exact_text_match_override_hard_conflicts() -> None:
    materials = (
        _rated_material("Q", description="identical fuse"),
        *(
            _rated_material(
                f"A{index}",
                description="identical fuse",
                Acting="fast",
                **{"Blow Characteristic": "fast"},
            )
            for index in range(5)
        ),
        *(
            _rated_material(f"B{index}", description="different family")
            for index in range(5)
        ),
    )

    query = next(
        result
        for result in rank_business_alternatives(materials)
        if result.part_id == "Q"
    )

    assert query.mode == "strict_hybrid"
    assert query.status == "insufficient_evidence"
    assert query.alternatives == ()
    assert {item.part_id for item in query.excluded} == {
        f"A{index}" for index in range(5)
    }


def test_blank_descriptions_use_structured_only_and_require_five_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete = (
        _rated_material("Q", description=""),
        *(_rated_material(part_id) for part_id in "ABCDEF"),
        _material("Z"),
    )
    lexical_inputs: list[tuple[Mapping[str, str], ...]] = []
    original_ranker = hybrid_module.rank_hybrid_alternatives

    def track_lexical_inputs(
        materials: Sequence[Mapping[str, str]], **kwargs: object
    ) -> tuple[HybridRetrievalResult, ...]:
        lexical_inputs.append(tuple(materials))
        return original_ranker(materials, **kwargs)

    monkeypatch.setattr(hybrid_module, "rank_hybrid_alternatives", track_lexical_inputs)

    query = next(
        result
        for result in rank_business_alternatives(complete)
        if result.part_id == "Q"
    )
    reversed_query = next(
        result
        for result in rank_business_alternatives(tuple(reversed(complete)))
        if result.part_id == "Q"
    )

    assert query == reversed_query
    assert query.schema_version == "2.0"
    assert query.mode == "structured_only"
    assert query.status == "ok"
    assert [item.part_id for item in query.alternatives] == list("ABCDE")
    assert all(item.mode == "structured_only" for item in query.alternatives)
    assert all(item.text.method == "structured_fallback" for item in query.alternatives)
    assert lexical_inputs
    assert all(
        material[DESCRIPTION_COLUMN].strip()
        for batch in lexical_inputs
        for material in batch
    )

    partial = rank_business_alternatives(complete[:3])[0]
    assert partial.status == "review_required"
    assert partial.reason == "fewer_than_five_defensible_candidates"

    all_blank = tuple(_rated_material(part_id, description="") for part_id in "QABCDEF")
    assert rank_business_alternatives(all_blank)[0].status == "ok"


def test_structured_only_abstains_on_sparse_or_conflicting_evidence() -> None:
    sparse = (
        _material("Q", description=""),
        *(_rated_material(item) for item in "ABCDEF"),
    )
    sparse_query = rank_business_alternatives(sparse)[0]
    assert sparse_query.status == "insufficient_evidence"
    assert sparse_query.reason == "query_structured_coverage_below_minimum"

    materials = (
        _rated_material("Q", description=""),
        *(
            _rated_material(
                f"A{index}",
                Acting="fast",
                **{"Blow Characteristic": "fast"},
            )
            for index in range(6)
        ),
    )
    conflict_query = rank_business_alternatives(materials)[0]
    assert conflict_query.status == "insufficient_evidence"
    assert conflict_query.alternatives == ()
    assert len(conflict_query.excluded) == 6


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
    assert parse_quantity("250 V (AC)", kind="voltage").mode == "ac"
    assert parse_quantity("250 V (DC)", kind="voltage").mode == "dc"
    assert parse_quantity("250 V ( AC )", kind="voltage").mode == "ac"
    assert parse_quantity("250 V ( DC )", kind="voltage").mode == "dc"


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


@pytest.mark.parametrize(
    ("raw", "kind"),
    (("1 MA", "current"), ("1 MV", "voltage")),
)
def test_quantity_parser_rejects_unreviewed_uppercase_si_prefixes(
    raw: str, kind: Literal["current", "voltage", "length"]
) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        parse_quantity(raw, kind=kind)


def test_range_parser_requires_paired_delimiters_and_preserves_exclusivity() -> None:
    parsed = parse_quantity("(1-2) A", kind="current")

    assert (parsed.lower, parsed.upper) == (1.0, 2.0)
    assert parsed.lower_inclusive is False
    assert parsed.upper_inclusive is False
    for malformed in ("[1-2 A", "1-2] A"):
        with pytest.raises(ValueError, match="unsupported"):
            parse_quantity(malformed, kind="current")
    for empty in ("[2-2) A", "(2-2] A", "(2-2) A"):
        with pytest.raises(ValueError, match="empty"):
            parse_quantity(empty, kind="current")
    assert parse_quantity("[2-2] A", kind="current").lower == 2.0


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

    qualifier_conflict = _rated_material(
        "R",
        **{"Rated Current (A)": "maximum 2A"},
    )
    qualifier_attributes = {
        item.name: item for item in parse_material_attributes(qualifier_conflict)
    }
    assert qualifier_attributes["current"].state == "conflict"


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


def test_overlapping_numeric_ranges_are_not_proven_hard_conflicts() -> None:
    materials = (
        _material("Q", **{"Current Rating": "1-100A"}),
        _material("A", **{"Current Rating": "1A"}),
        *(_material(part_id, **{"Current Rating": "1-100A"}) for part_id in "BCDEF"),
    )

    query = next(
        result
        for result in rank_hybrid_alternatives(materials)
        if result.part_id == "Q"
    )

    assert "A" not in {item.part_id for item in query.excluded}
    assert "A" in {item.part_id for item in query.alternatives}


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

    blank = rank_hybrid_alternatives(
        (_material("Q", description=""), _rated_material("A"))
    )[0]
    assert blank.status == "insufficient_description"


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


def test_quantity_and_dimension_parsers_cover_safety_boundaries() -> None:
    assert parse_quantity("minimum 2A", kind="current").qualifier == "minimum"
    with pytest.raises(ValueError, match="both AC and DC"):
        parse_quantity("250 VAC DC", kind="voltage")
    with pytest.raises(ValueError, match="magnitude"):
        parse_quantity("10000000000000A", kind="current")
    with pytest.raises(ValueError, match="two or three"):
        parse_dimensions("20mm")
    with pytest.raises(ValueError, match="scalar"):
        parse_dimensions("1-2mm x 5mm")
    with pytest.raises(ValueError, match="must be text"):
        parse_category(1, aliases={})  # type: ignore[arg-type]


def test_contradictory_sources_and_soft_penalties_remain_visible() -> None:
    contradictory = assess_compatibility(
        _rated_material("Q", **{"Rated Current (A)": "5A"}),
        _rated_material("A"),
    )
    assert contradictory.outcome == "conflict"
    assert contradictory.conflicts[0].code == "contradictory_source_fields"

    materials = (
        _rated_material("Q"),
        _rated_material("A", **{"Fuse Material": "ceramic"}),
        _rated_material("B", **{"Current Rating": "maximum 2A"}),
        *(_rated_material(part_id) for part_id in "CDEFG"),
    )
    query = rank_hybrid_alternatives(materials)[0]
    material = next(item for item in query.alternatives if item.part_id == "A")
    qualifier = next(item for item in query.alternatives if item.part_id == "B")
    assert material.conflicts[0].code == "categorical_mismatch"
    assert material.conflicts[0].hard is False
    assert qualifier.penalties[0].code == "qualifier_mismatch"


def test_internal_comparisons_cover_closed_union_boundaries() -> None:
    spec = hybrid_module._SPECS[0]
    quantity = parse_quantity("1A", kind="current")
    category = Category("glass", "glass")
    score, conflict = hybrid_module._compare(spec, quantity, category)
    assert score == 0.0
    assert conflict is not None and conflict.code == "value_kind_mismatch"

    dimensions_spec = next(
        item for item in hybrid_module._SPECS if item.name == "dimensions"
    )
    score, conflict = hybrid_module._compare(
        dimensions_spec,
        Dimensions("2x3", (2.0, 3.0)),
        Dimensions("2x3x4", (2.0, 3.0, 4.0)),
    )
    assert score == 0.0
    assert conflict is not None and conflict.code == "dimension_axis_mismatch"

    with pytest.raises(TypeError, match="unsupported structured"):
        hybrid_module._compare(spec, object(), object())  # type: ignore[arg-type]

    zero = parse_quantity("0A", kind="current")
    one = parse_quantity("1A", kind="current")
    _, conflict = hybrid_module._compare(spec, zero, one)
    assert conflict is not None and conflict.code == "numeric_hard_conflict"
    assert hybrid_module._positive_similarity(0.0, 1.0) == 0.0
    assert hybrid_module._equivalent(quantity, category) is False
    assert hybrid_module._equivalent(
        Dimensions("a", (1.0, 2.0)), Dimensions("b", (1.0, 2.0))
    )
    assert hybrid_module._equivalent(category, Category("ceramic", "ceramic")) is False
    assert hybrid_module._equivalent(object(), object()) is False  # type: ignore[arg-type]


def test_compatibility_policy_rejects_corrupt_and_ambiguous_settings(
    tmp_path: Path,
) -> None:
    source = Path(__file__).parents[1] / "evals" / "compatibility-policy.yaml"
    base = json.loads(source.read_text(encoding="utf-8"))

    invalid_roots: tuple[tuple[object, str], ...] = (
        ([], "must be an object"),
        ({**base, "schema_version": "2.0"}, "schema version"),
        ({**base, "minimum_structured_coverage": True}, "must be numeric"),
        ({**base, "minimum_structured_coverage": 2.0}, "between zero and one"),
        ({**base, "required_candidates": 4}, "exactly five"),
        ({**base, "rules": {}}, "must be an array"),
    )
    for index, (payload, message) in enumerate(invalid_roots):
        path = tmp_path / f"invalid-{index}.yaml"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_compatibility_policy(path)

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot read"):
        load_compatibility_policy(malformed)
    with pytest.raises(ValueError, match="cannot read"):
        load_compatibility_policy(tmp_path / "absent.yaml")


def test_compatibility_policy_validates_every_rule_contract() -> None:
    policy = DEFAULT_COMPATIBILITY_POLICY
    with pytest.raises(ValueError, match="schema version"):
        hybrid_module._validate_policy(replace(policy, schema_version="2.0"))
    with pytest.raises(ValueError, match="coverage"):
        hybrid_module._validate_policy(
            replace(policy, minimum_structured_coverage=float("nan"))
        )
    with pytest.raises(ValueError, match="exactly five"):
        hybrid_module._validate_policy(replace(policy, required_candidates=4))
    with pytest.raises(ValueError, match="field order"):
        hybrid_module._validate_policy(
            replace(policy, rules=tuple(reversed(policy.rules)))
        )

    first = policy.rules[0]
    invalid_rules = (
        (replace(first, weight=0.0), "weight"),
        (replace(first, hard_ratio=1.0), "hard ratio"),
        (replace(first, supported_values=("x", "x")), "supported values"),
    )
    for rule, message in invalid_rules:
        with pytest.raises(ValueError, match=message):
            hybrid_module._validate_policy(
                replace(policy, rules=(rule, *policy.rules[1:]))
            )


@pytest.mark.parametrize(
    ("value", "message"),
    (
        ([], "must be an object"),
        ({1: "x"}, "must be an object"),
        (
            {
                "name": "",
                "weight": 1,
                "hard_ratio": None,
                "hard_category": False,
                "never_relax": False,
            },
            "name is invalid",
        ),
        (
            {
                "name": "x",
                "weight": True,
                "hard_ratio": None,
                "hard_category": False,
                "never_relax": False,
            },
            "weight must be numeric",
        ),
        (
            {
                "name": "x",
                "weight": 1,
                "hard_ratio": "bad",
                "hard_category": False,
                "never_relax": False,
            },
            "hard ratio is invalid",
        ),
        (
            {
                "name": "x",
                "weight": 1,
                "hard_ratio": None,
                "hard_category": "no",
                "never_relax": False,
            },
            "flags must be boolean",
        ),
        (
            {
                "name": "x",
                "weight": 1,
                "hard_ratio": None,
                "hard_category": False,
                "never_relax": False,
                "supported_values": [""],
            },
            "supported values are invalid",
        ),
    ),
)
def test_policy_rule_loader_rejects_invalid_shapes(value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        hybrid_module._load_policy_rule(value, 0)
