from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from material_similarity.cli import main
from material_similarity.data import (
    DESCRIPTION_COLUMN,
    MATERIAL_COLUMNS,
    PART_ID_COLUMN,
)


def _catalog(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MATERIAL_COLUMNS, delimiter=";")
        writer.writeheader()
        for index in range(6):
            row = dict.fromkeys(MATERIAL_COLUMNS, "")
            row[PART_ID_COLUMN] = str(index)
            row[DESCRIPTION_COLUMN] = f"ceramic fuse family {index}"
            writer.writerow(row)


def test_cli_serializes_one_part_and_the_complete_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = tmp_path / "Fuse.csv"
    _catalog(catalog)

    assert main([str(catalog), "--part-id", "2"]) == 0
    captured = capsys.readouterr().out
    one = json.loads(captured)
    assert one["part_id"] == "2"
    assert len(one["alternatives"]) == 5

    output = tmp_path / "all.json"
    assert main([str(catalog), "--output", str(output)]) == 0
    all_results = json.loads(output.read_text(encoding="utf-8"))
    assert [result["part_id"] for result in all_results] == [str(i) for i in range(6)]


def test_cli_exposes_hybrid_only_by_explicit_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = tmp_path / "Fuse.csv"
    _catalog(catalog)

    assert main([str(catalog), "--part-id", "2"]) == 0
    text_result = json.loads(capsys.readouterr().out)
    assert "excluded" not in text_result
    assert "mode" not in text_result["alternatives"][0]

    assert main([str(catalog), "--mode", "hybrid", "--part-id", "2"]) == 0
    hybrid_result = json.loads(capsys.readouterr().out)
    assert hybrid_result["excluded"] == []
    assert all(item["mode"] == "text_only" for item in hybrid_result["alternatives"])
    assert all("text" in item for item in hybrid_result["alternatives"])
