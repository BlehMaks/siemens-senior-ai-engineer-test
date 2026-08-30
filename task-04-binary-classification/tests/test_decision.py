from __future__ import annotations

import json
from pathlib import Path

import pytest

from binary_classification.decision import (
    EXAMPLE_SCENARIOS,
    DecisionScenario,
    choose_decision,
    evaluate_decision_policy,
    load_cost_config,
)


def test_example_scenarios_choose_different_actions_from_the_same_probability() -> None:
    probability = 0.2

    balanced = choose_decision(probability, EXAMPLE_SCENARIOS["balanced-review"])
    miss_averse = choose_decision(probability, EXAMPLE_SCENARIOS["miss-averse-review"])

    assert balanced.action == "class_0"
    assert miss_averse.action == "manual_review"
    assert balanced.expected_cost == pytest.approx(0.2)
    assert miss_averse.expected_cost == pytest.approx(0.75)


def test_cost_ties_are_explicit_and_prefer_visible_review() -> None:
    scenario = DecisionScenario(
        name="tie",
        false_positive_cost=1.0,
        false_negative_cost=1.0,
        review_cost=0.5,
    )

    decision = choose_decision(0.5, scenario)

    assert decision.action == "manual_review"
    assert decision.tied_actions == ("manual_review", "class_0", "class_1")


def test_policy_metrics_separate_review_from_automatic_errors() -> None:
    scenario = DecisionScenario(
        name="mixed",
        false_positive_cost=1.0,
        false_negative_cost=1.0,
        review_cost=0.2,
    )

    metrics = evaluate_decision_policy(
        [False, False, True, True], [0.1, 0.7, 0.3, 0.9], scenario
    )

    assert metrics.class_0_count == 1
    assert metrics.class_1_count == 1
    assert metrics.review_count == 2
    assert metrics.automatic_decision_coverage == pytest.approx(0.5)
    assert metrics.review_rate == pytest.approx(0.5)
    assert metrics.automatic_error_rate == 0.0
    assert metrics.automatic_precision == 1.0
    assert metrics.automatic_recall == 1.0
    assert metrics.automatic_f1 == 1.0
    assert metrics.to_dict()["scenario"]["name"] == "mixed"


def test_all_review_policy_reports_no_hidden_class_or_error_rate() -> None:
    scenario = DecisionScenario(
        name="review",
        false_positive_cost=1.0,
        false_negative_cost=1.0,
        review_cost=0.0,
    )

    metrics = evaluate_decision_policy([False, True], [0.1, 0.9], scenario)

    assert metrics.review_count == 2
    assert metrics.automatic_decision_coverage == 0.0
    assert metrics.automatic_error_rate is None
    assert metrics.automatic_precision is None
    assert metrics.automatic_recall is None
    assert metrics.automatic_f1 is None


@pytest.mark.parametrize("cost", [-1.0, float("nan"), float("inf"), True])
def test_scenario_rejects_invalid_costs(cost: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        DecisionScenario(
            name="invalid",
            false_positive_cost=cost,
            false_negative_cost=1.0,
            review_cost=1.0,
        )


def test_scenario_rejects_non_numeric_json_cost() -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        DecisionScenario.from_dict(
            {
                "name": "invalid",
                "false_positive_cost": "one",
                "false_negative_cost": 1.0,
                "review_cost": 1.0,
            }
        )


@pytest.mark.parametrize(
    ("name", "negative_label", "positive_label"),
    [
        ("", "y", "n"),
        ("valid", "", "n"),
        ("valid", "same", "same"),
    ],
)
def test_scenario_rejects_ambiguous_identity(
    name: str, negative_label: str, positive_label: str
) -> None:
    with pytest.raises(ValueError, match=r"name|labels"):
        DecisionScenario(
            name=name,
            false_positive_cost=1.0,
            false_negative_cost=1.0,
            review_cost=1.0,
            negative_label=negative_label,
            positive_label=positive_label,
        )


@pytest.mark.parametrize("probability", [-0.1, 1.1, float("nan")])
def test_decision_rejects_invalid_probability(probability: float) -> None:
    with pytest.raises(ValueError, match="probability"):
        choose_decision(probability, EXAMPLE_SCENARIOS["balanced-review"])


def test_policy_rejects_invalid_vectors() -> None:
    scenario = EXAMPLE_SCENARIOS["balanced-review"]
    with pytest.raises(ValueError, match="equal-length"):
        evaluate_decision_policy([True], [0.1, 0.2], scenario)
    with pytest.raises(ValueError, match="cannot be empty"):
        evaluate_decision_policy([], [], scenario)


def test_versioned_cost_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "costs.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scenarios": [
                    {
                        "name": "owner-confirmed",
                        "false_positive_cost": 2.0,
                        "false_negative_cost": 7.0,
                        "review_cost": 0.5,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    scenarios = load_cost_config(path)

    assert scenarios[0].name == "owner-confirmed"
    assert scenarios[0].example_only is False


@pytest.mark.parametrize(
    "value",
    [
        {"schema_version": "2.0", "scenarios": []},
        {"schema_version": "1.0", "scenarios": []},
        {"schema_version": "1.0", "scenarios": ["not-an-object"]},
        {
            "schema_version": "1.0",
            "scenarios": [
                {
                    "name": "duplicate",
                    "false_positive_cost": 1,
                    "false_negative_cost": 1,
                    "review_cost": 1,
                },
                {
                    "name": "duplicate",
                    "false_positive_cost": 1,
                    "false_negative_cost": 1,
                    "review_cost": 1,
                },
            ],
        },
        {
            "schema_version": "1.0",
            "scenarios": [{"name": "missing-costs", "typo": 1}],
        },
    ],
)
def test_cost_config_rejects_invalid_contract(tmp_path: Path, value: object) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match=r"schema|scenarios|scenario|fields|unique"):
        load_cost_config(path)


def test_cost_config_wraps_read_and_parse_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Cannot read"):
        load_cost_config(tmp_path / "missing.json")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read"):
        load_cost_config(invalid)
