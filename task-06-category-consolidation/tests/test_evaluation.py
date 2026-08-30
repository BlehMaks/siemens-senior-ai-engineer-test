from __future__ import annotations

import json
import runpy
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

import category_consolidation.evaluation as evaluation
from category_consolidation.evaluation import (
    build_comparison_report,
    main,
    render_markdown,
    write_comparison_report,
)


def test_report_recomputes_expected_aggregates() -> None:
    report = build_comparison_report(benchmark_rows=100, benchmark_iterations=1)
    baseline = report["assignment_baseline"]
    extension = report["business_extension"]

    assert report["schema_version"] == 1
    assert baseline["single_column_output_equivalent"] is True
    assert extension["alias_normalization"]["mode_status"] == "not_implemented"
    assert all(extension["sklearn_checks"].values())
    assert extension["artifact"]["fingerprint"].startswith("sha256:")
    for column in ("region", "channel"):
        baseline_metrics = baseline["by_column"][column]
        extension_metrics = extension["by_column"][column]
        delta = report["delta"]["comparable_measures"][column]
        assert delta["fallback_count"] == (
            extension_metrics["fallback_count"] - baseline_metrics["fallback_count"]
        )
        assert delta["retained_category_count"] == (
            extension_metrics["retained_category_count"]
            - baseline_metrics["retained_category_count"]
        )


def test_json_and_markdown_are_written_from_same_report(tmp_path: Path) -> None:
    json_path, markdown_path = write_comparison_report(
        tmp_path, benchmark_rows=100, benchmark_iterations=1
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert markdown == render_markdown(report)
    assert report["metadata"]["data_fingerprint"] in markdown
    assert report["business_extension"]["artifact"]["fingerprint"] in markdown
    assert "not_implemented" in markdown


@pytest.mark.parametrize(
    ("rows", "iterations", "message"),
    [(0, 1, "benchmark_rows"), (100_001, 1, "benchmark_rows"), (10, 0, "iterations")],
)
def test_benchmark_bounds_are_validated(
    rows: int, iterations: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_comparison_report(benchmark_rows=rows, benchmark_iterations=iterations)


def test_cli_writes_both_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = main(
        [
            "--output-dir",
            str(tmp_path),
            "--benchmark-rows",
            "100",
            "--benchmark-iterations",
            "1",
        ]
    )

    assert result == 0
    assert (tmp_path / "baseline-vs-extension.json").is_file()
    assert (tmp_path / "baseline-vs-extension.md").is_file()
    assert "Wrote" in capsys.readouterr().out


def test_missing_package_version_uses_source_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_version(package: str) -> str:
        del package
        raise PackageNotFoundError

    monkeypatch.setattr(evaluation, "version", missing_version)

    assert evaluation._installed_version("missing") == "source-tree"


def test_module_entry_point_runs_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluation",
            "--output-dir",
            str(tmp_path),
            "--benchmark-rows",
            "10",
            "--benchmark-iterations",
            "1",
        ],
    )

    loaded_module = sys.modules.pop("category_consolidation.evaluation")
    try:
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("category_consolidation.evaluation", run_name="__main__")
    finally:
        sys.modules["category_consolidation.evaluation"] = loaded_module

    assert exc_info.value.code == 0
    assert (tmp_path / "baseline-vs-extension.json").is_file()
