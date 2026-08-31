from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

import binary_classification.evaluate as evaluate_module
from binary_classification.evaluate import _error_slices, render_comparison_markdown


@pytest.mark.parametrize(
    ("literal", "other", "other_bucket"),
    [
        ("__MISSING__", None, "bucket:missing"),
        ("__OTHER__", "unseen", "bucket:other"),
    ],
)
def test_error_slice_buckets_do_not_collide_with_literal_categories(
    literal: str, other: object, other_bucket: str
) -> None:
    training = pd.DataFrame(
        {
            "category": [literal] * 6 + ["ordinary"] * 5,
            "id": range(11),
            "Class": ["n", "y"] * 5 + ["n"],
        }
    )
    holdout = pd.DataFrame(
        {
            "category": [literal, other],
            "id": [20, 21],
            "Class": ["n", "y"],
        }
    )

    slices = _error_slices(
        training,
        holdout,
        holdout["Class"].eq("n"),
        np.array([0.9, 0.1]),
        0.5,
        ("category",),
    )
    category_slices = [item for item in slices if item.dimension == "category"]

    assert {item.value for item in category_slices} == {
        f"value:{literal}",
        other_bucket,
    }
    assert [item.rows for item in category_slices] == [1, 1]


def test_cli_requires_explicit_scenario_selection_for_extension(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(
        part1: Path,
        part2: Path,
        output: Path,
        *,
        seed: int,
        cost_scenarios: tuple[Any, ...],
        review_queue_path: Path | None,
        owner_include_source_ids: bool,
    ) -> SimpleNamespace:
        captured.update(
            {
                "part1": part1,
                "part2": part2,
                "output": output,
                "seed": seed,
                "cost_scenarios": cost_scenarios,
                "review_queue_path": review_queue_path,
                "owner_include_source_ids": owner_include_source_ids,
            }
        )
        return SimpleNamespace(
            selected_model="weighted_logistic",
            selected_threshold=0.5,
            holdout_at_selected_threshold=SimpleNamespace(pr_auc=0.75),
        )

    monkeypatch.setattr(evaluate_module, "run_experiment", fake_run)

    status = evaluate_module.main(
        [
            "--part1",
            "part1.csv",
            "--part2",
            "part2.csv",
            "--output-dir",
            "output",
            "--seed",
            "7",
            "--cost-scenario",
            "balanced-review",
            "--review-queue",
            "local-review.json",
            "--owner-include-source-ids",
        ]
    )

    assert status == 0
    assert captured["seed"] == 7
    assert [scenario.name for scenario in captured["cost_scenarios"]] == [
        "balanced-review"
    ]
    assert captured["review_queue_path"] == Path("local-review.json")
    assert captured["owner_include_source_ids"] is True
    assert "selected=weighted_logistic" in capsys.readouterr().out


def test_module_entrypoint_exposes_help(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["binary_classification.evaluate", "--help"])

    with (
        pytest.warns(RuntimeWarning, match="found in sys.modules"),
        pytest.raises(SystemExit) as exit_info,
    ):
        runpy.run_module("binary_classification.evaluate", run_name="__main__")

    assert exit_info.value.code == 0


def test_committed_markdown_is_rendered_from_committed_json() -> None:
    reports = Path(__file__).parents[1] / "reports"
    report = json.loads(
        (reports / "baseline-vs-extension.json").read_text(encoding="utf-8")
    )

    assert (reports / "baseline-vs-extension.md").read_text(
        encoding="utf-8"
    ) == render_comparison_markdown(report)
