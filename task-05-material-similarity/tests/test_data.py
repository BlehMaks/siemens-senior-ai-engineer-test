from __future__ import annotations

import csv
from pathlib import Path

import pytest

from material_similarity.data import (
    DESCRIPTION_COLUMN,
    MATERIAL_COLUMNS,
    PART_ID_COLUMN,
    MaterialDataError,
    load_materials,
    profile_materials,
)


def _row(part_id: str, description: str, **values: str) -> dict[str, str]:
    row = dict.fromkeys(MATERIAL_COLUMNS, "")
    row.update(values)
    row[PART_ID_COLUMN] = part_id
    row[DESCRIPTION_COLUMN] = description
    return row


def _write_catalog(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MATERIAL_COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def test_load_materials_preserves_explicit_blanks_and_na_text(tmp_path: Path) -> None:
    source = tmp_path / "Fuse.csv"
    _write_catalog(source, [_row(" A1 ", ""), _row("A2", "NA")])

    materials = load_materials(source)

    assert [row[PART_ID_COLUMN] for row in materials] == ["A1", "A2"]
    assert [row[DESCRIPTION_COLUMN] for row in materials] == ["", "NA"]


@pytest.mark.parametrize("part_ids", [["", "A2"], ["A1", "A1"]])
def test_load_materials_rejects_invalid_identifiers(
    tmp_path: Path, part_ids: list[str]
) -> None:
    source = tmp_path / "Fuse.csv"
    _write_catalog(source, [_row(part_ids[0], "one"), _row(part_ids[1], "two")])

    with pytest.raises(MaterialDataError, match="PART_ID"):
        load_materials(source)


def test_load_materials_rejects_wrong_schema(tmp_path: Path) -> None:
    source = tmp_path / "Fuse.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MATERIAL_COLUMNS[:-1], delimiter=";")
        writer.writeheader()
        writer.writerow(dict.fromkeys(MATERIAL_COLUMNS[:-1], ""))

    with pytest.raises(MaterialDataError, match=r"Rated Voltage\(DC\)"):
        load_materials(source)


def test_load_materials_rejects_unterminated_csv_quote(tmp_path: Path) -> None:
    source = tmp_path / "Fuse.csv"
    source.write_text(
        ";".join(MATERIAL_COLUMNS) + '\nA1;"unterminated', encoding="utf-8"
    )

    with pytest.raises(MaterialDataError, match="malformed CSV"):
        load_materials(source)


def test_profile_exposes_blanks_duplicates_sparsity_categories_and_units() -> None:
    materials = [
        _row("A1", "Fuse 5 A", Acting="Fast", **{"Current Rating": "5A"}),
        _row("A2", "Fuse 5 A", Acting="Slow", **{"Current Rating": "5 amp"}),
        _row("A3", "", Acting="Fast", **{"Current Rating": "6.3@(CSA/UL)A"}),
        _row("A4", "  ", Acting="", **{"Current Rating": "n/a"}),
    ]

    profile = profile_materials(materials)

    assert profile.row_count == 4
    assert profile.column_count == 32
    assert profile.unique_part_id_count == 4
    assert profile.blank_description_count == 2
    assert profile.duplicate_description_count == 1
    assert profile.duplicate_description_group_count == 1
    assert profile.column("Acting").unique_nonempty_count == 2
    assert profile.column("Current Rating").unit_bearing_count == 3
    assert DESCRIPTION_COLUMN in profile.sparse_columns
    assert PART_ID_COLUMN not in profile.sparse_columns


def test_profile_rejects_empty_catalog() -> None:
    with pytest.raises(MaterialDataError, match="empty"):
        profile_materials([])
