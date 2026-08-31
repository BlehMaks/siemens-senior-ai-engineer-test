"""Typed cost scenarios and explicit three-way decision policies."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

DecisionAction = Literal["class_0", "manual_review", "class_1"]
_ACTION_ORDER: tuple[DecisionAction, ...] = (
    "manual_review",
    "class_0",
    "class_1",
)
_CONFIG_SCHEMA_VERSION = "1.0"
_REVIEW_QUEUE_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class DecisionScenario:
    """Business-supplied relative costs for one named decision scenario."""

    name: str
    false_positive_cost: float
    false_negative_cost: float
    review_cost: float
    negative_label: str = "y"
    positive_label: str = "n"
    example_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("scenario name cannot be empty")
        costs = (
            self.false_positive_cost,
            self.false_negative_cost,
            self.review_cost,
        )
        if any(
            isinstance(cost, bool)
            or not isinstance(cost, (int, float, np.integer, np.floating))
            or not np.isfinite(cost)
            or cost < 0
            for cost in costs
        ):
            raise ValueError("decision costs must be finite and non-negative")
        if (
            not isinstance(self.negative_label, str)
            or not isinstance(self.positive_label, str)
            or not self.negative_label.strip()
            or not self.positive_label.strip()
            or self.negative_label == self.positive_label
        ):
            raise ValueError("decision labels must be distinct non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        """Return the deterministic JSON representation used in reports."""

        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DecisionScenario:
        """Parse one strict scenario object without silently accepting typos."""

        allowed = {
            "name",
            "false_positive_cost",
            "false_negative_cost",
            "review_cost",
            "negative_label",
            "positive_label",
            "example_only",
        }
        unknown = set(value) - allowed
        required = {
            "name",
            "false_positive_cost",
            "false_negative_cost",
            "review_cost",
        }
        missing = required - set(value)
        if unknown or missing:
            raise ValueError(
                f"invalid scenario fields: missing={sorted(missing)}, "
                f"unknown={sorted(unknown)}"
            )
        return cls(**value)


EXAMPLE_SCENARIOS: dict[str, DecisionScenario] = {
    "balanced-review": DecisionScenario(
        name="balanced-review",
        false_positive_cost=1.0,
        false_negative_cost=1.0,
        review_cost=0.30,
        example_only=True,
    ),
    "miss-averse-review": DecisionScenario(
        name="miss-averse-review",
        false_positive_cost=1.0,
        false_negative_cost=8.0,
        review_cost=0.75,
        example_only=True,
    ),
}


@dataclass(frozen=True, slots=True)
class Decision:
    """One selected action with all tied minimum-cost actions exposed."""

    action: DecisionAction
    expected_cost: float
    tied_actions: tuple[DecisionAction, ...]


@dataclass(frozen=True, slots=True)
class DecisionMetrics:
    """Aggregate policy behavior without exposing source rows."""

    scenario: DecisionScenario
    rows: int
    class_0_count: int
    class_1_count: int
    review_count: int
    automatic_decision_coverage: float
    review_rate: float
    automatic_error_rate: float | None
    automatic_precision: float | None
    automatic_recall: float | None
    automatic_f1: float | None
    true_negative: int
    false_positive: int
    false_negative: int
    true_positive: int
    mean_expected_cost: float
    mean_realized_cost: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe report object."""

        return asdict(self)


def choose_decision(probability: float, scenario: DecisionScenario) -> Decision:
    """Minimize expected cost, preferring visible review when it is tied."""

    if not np.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be finite and between 0 and 1")
    costs: dict[DecisionAction, float] = {
        "class_0": probability * scenario.false_negative_cost,
        "manual_review": scenario.review_cost,
        "class_1": (1.0 - probability) * scenario.false_positive_cost,
    }
    minimum = min(costs.values())
    tied = tuple(
        action
        for action in _ACTION_ORDER
        if np.isclose(costs[action], minimum, rtol=0.0, atol=1e-12)
    )
    return Decision(action=tied[0], expected_cost=float(minimum), tied_actions=tied)


def evaluate_decision_policy(
    target: NDArray[np.bool_] | list[bool],
    probabilities: NDArray[np.float64] | list[float],
    scenario: DecisionScenario,
) -> DecisionMetrics:
    """Evaluate one policy using labels only for aggregate realized outcomes."""

    truth = np.asarray(target, dtype=bool)
    scores = np.asarray(probabilities, dtype="float64")
    if truth.ndim != 1 or scores.ndim != 1 or len(truth) != len(scores):
        raise ValueError("target and probabilities must be equal-length vectors")
    if not len(truth):
        raise ValueError("target and probabilities cannot be empty")
    decisions = tuple(choose_decision(float(score), scenario) for score in scores)
    actions = np.asarray([decision.action for decision in decisions], dtype=object)
    class_0 = actions == "class_0"
    class_1 = actions == "class_1"
    review = actions == "manual_review"
    automatic = ~review
    errors = (class_0 & truth) | (class_1 & ~truth)
    true_negative = int((class_0 & ~truth).sum())
    false_positive = int((class_1 & ~truth).sum())
    false_negative = int((class_0 & truth).sum())
    true_positive = int((class_1 & truth).sum())
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    precision = true_positive / precision_denominator if precision_denominator else None
    recall = true_positive / recall_denominator if recall_denominator else None
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    realized = (
        class_0 * truth * scenario.false_negative_cost
        + class_1 * ~truth * scenario.false_positive_cost
        + review * scenario.review_cost
    )
    automatic_count = int(automatic.sum())
    return DecisionMetrics(
        scenario=scenario,
        rows=len(truth),
        class_0_count=int(class_0.sum()),
        class_1_count=int(class_1.sum()),
        review_count=int(review.sum()),
        automatic_decision_coverage=automatic_count / len(truth),
        review_rate=float(review.mean()),
        automatic_error_rate=(
            float(errors[automatic].mean()) if automatic_count else None
        ),
        automatic_precision=precision,
        automatic_recall=recall,
        automatic_f1=f1,
        true_negative=true_negative,
        false_positive=false_positive,
        false_negative=false_negative,
        true_positive=true_positive,
        mean_expected_cost=float(
            np.mean([decision.expected_cost for decision in decisions])
        ),
        mean_realized_cost=float(np.mean(realized)),
    )


def build_review_queue(
    probabilities: NDArray[np.float64] | list[float],
    scenario: DecisionScenario,
    *,
    source_ids: Sequence[str | int] | None = None,
) -> dict[str, Any]:
    """Build a target-free, feature-free queue for one operational scenario."""

    scores = np.asarray(probabilities, dtype="float64")
    if scores.ndim != 1 or not len(scores):
        raise ValueError("review probabilities must be a non-empty vector")
    if source_ids is not None and len(source_ids) != len(scores):
        raise ValueError("source IDs and probabilities must have equal length")

    candidates: list[tuple[float, float, int, dict[str, Any]]] = []
    for position, score in enumerate(scores):
        probability = float(score)
        decision = choose_decision(probability, scenario)
        if decision.action != "manual_review":
            continue
        automatic_cost = min(
            probability * scenario.false_negative_cost,
            (1.0 - probability) * scenario.false_positive_cost,
        )
        cost_avoidance = max(0.0, automatic_cost - scenario.review_cost)
        uncertainty = 1.0 - 2.0 * abs(probability - 0.5)
        item: dict[str, Any] = {
            "priority": {
                "cost_avoidance": cost_avoidance,
                "uncertainty": uncertainty,
            },
            "reason": (
                "manual_review_tied_for_lowest_expected_cost"
                if len(decision.tied_actions) > 1
                else "manual_review_has_lowest_expected_cost"
            ),
        }
        if source_ids is not None:
            item["source_id"] = source_ids[position]
        candidates.append((-cost_avoidance, -uncertainty, position, item))

    candidates.sort(key=lambda candidate: candidate[:3])
    items = []
    for rank, candidate in enumerate(candidates, start=1):
        item = candidate[3]
        items.append({"review_id": f"review-{rank:06d}", **item})
    return {
        "schema_version": _REVIEW_QUEUE_SCHEMA_VERSION,
        "mode": "human_labeling_aid",
        "scenario": scenario.name,
        "source_ids_included": source_ids is not None,
        "ordering": [
            "cost_avoidance_desc",
            "uncertainty_desc",
            "source_position_asc",
        ],
        "items": items,
    }


def load_cost_config(path: str | Path) -> tuple[DecisionScenario, ...]:
    """Load a versioned JSON cost configuration from a trusted local path."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read cost configuration: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != _CONFIG_SCHEMA_VERSION
    ):
        raise ValueError(
            f"cost configuration schema_version must be {_CONFIG_SCHEMA_VERSION}"
        )
    raw_scenarios = value.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("cost configuration must contain a non-empty scenarios list")
    if not all(isinstance(item, dict) for item in raw_scenarios):
        raise ValueError("each cost scenario must be a JSON object")
    scenarios = tuple(DecisionScenario.from_dict(item) for item in raw_scenarios)
    names = [scenario.name for scenario in scenarios]
    if len(names) != len(set(names)):
        raise ValueError("cost scenario names must be unique")
    return scenarios
