import os
from pathlib import Path

import pandas as pd
import pytest

from binary_classification.data import DataContractError, load_training_data

PART1_HEADER = "BIB;COD;ERG;FAN;GJAH;LUK;MYR;NUS;PKD;RAS;id\n"
PART2_HEADER = "SIS;TOK;UIN;VOL;WET;KAT;XIN;Class;id\n"
PART1_ROWS = [
    "160;iii;www;80.0;iii;5.0;eee;800000.0;xxx;t;0\n",
    "153;uuu;aaa;200.0;rrr;0.0;mmm;2000000.0;xxx;;1\n",
]
PART2_ROWS = [
    "1.75;t;17.92;f;1;ccc;t;n;0\n",
    "0.29;f;16.92;f;0;ddd;f;y;1\n",
]


def _write(path: Path, header: str, rows: list[str]) -> Path:
    path.write_text(header + "".join(rows), encoding="utf-8")
    return path


def _valid_files(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _write(tmp_path / "part1.csv", PART1_HEADER, PART1_ROWS),
        _write(tmp_path / "part2.csv", PART2_HEADER, PART2_ROWS),
    )


def test_loads_semicolon_tables_and_joins_one_row_per_entity(tmp_path: Path) -> None:
    part1, part2 = _valid_files(tmp_path)

    dataset = load_training_data(part1, part2)

    assert dataset.frame["id"].tolist() == [0, 1]
    assert dataset.frame["Class"].tolist() == ["n", "y"]
    assert dataset.audit.entity_rows == 2


def test_unreadable_input_is_a_data_contract_error(tmp_path: Path) -> None:
    _, part2 = _valid_files(tmp_path)

    with pytest.raises(DataContractError, match="Could not read"):
        load_training_data(tmp_path / "missing.csv", part2)


def test_exact_duplicates_are_removed_and_join_trap_is_reported(tmp_path: Path) -> None:
    part1 = _write(tmp_path / "part1.csv", PART1_HEADER, [*PART1_ROWS, PART1_ROWS[0]])
    part2 = _write(tmp_path / "part2.csv", PART2_HEADER, [*PART2_ROWS, PART2_ROWS[0]])

    dataset = load_training_data(part1, part2)

    assert dataset.audit.part1_exact_duplicates == 1
    assert dataset.audit.part2_exact_duplicates == 1
    assert dataset.audit.naive_join_rows == 5
    assert dataset.audit.entity_rows == 2


def test_conflicting_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    conflicting = [*PART1_ROWS, PART1_ROWS[0].replace("160", "161", 1)]
    part1 = _write(tmp_path / "part1.csv", PART1_HEADER, conflicting)
    part2 = _write(tmp_path / "part2.csv", PART2_HEADER, PART2_ROWS)

    with pytest.raises(DataContractError, match=r"conflicting rows for ids \[0\]"):
        load_training_data(part1, part2)


def test_missing_entities_are_rejected(tmp_path: Path) -> None:
    part1 = _write(tmp_path / "part1.csv", PART1_HEADER, PART1_ROWS[:1])
    part2 = _write(tmp_path / "part2.csv", PART2_HEADER, PART2_ROWS)

    with pytest.raises(DataContractError, match="different entities"):
        load_training_data(part1, part2)


def test_wrong_delimiter_is_exposed_by_schema_validation(tmp_path: Path) -> None:
    part1 = _write(tmp_path / "part1.csv", PART1_HEADER.replace(";", ","), PART1_ROWS)
    part2 = _write(tmp_path / "part2.csv", PART2_HEADER, PART2_ROWS)

    with pytest.raises(DataContractError, match="Unexpected columns"):
        load_training_data(part1, part2)


def test_extra_fields_are_rejected_instead_of_becoming_an_index(tmp_path: Path) -> None:
    part1 = _write(
        tmp_path / "part1.csv",
        PART1_HEADER,
        [f"unexpected;{row}" for row in PART1_ROWS],
    )
    part2 = _write(tmp_path / "part2.csv", PART2_HEADER, PART2_ROWS)

    with pytest.raises(DataContractError, match="Unexpected field count"):
        load_training_data(part1, part2)


@pytest.mark.parametrize("invalid_id", ["", "one", "1.5", "--1", "+-1", "²", "①"])
def test_invalid_ids_are_rejected(tmp_path: Path, invalid_id: str) -> None:
    invalid_row = PART1_ROWS[1].rsplit(";", 1)[0] + f";{invalid_id}\n"
    part1 = _write(tmp_path / "part1.csv", PART1_HEADER, [PART1_ROWS[0], invalid_row])
    part2 = _write(tmp_path / "part2.csv", PART2_HEADER, PART2_ROWS)

    with pytest.raises(DataContractError, match="Invalid id values"):
        load_training_data(part1, part2)


def test_out_of_range_integer_id_is_rejected_without_wraparound(tmp_path: Path) -> None:
    large_id = str(2**64 - 1)
    part1_row = PART1_ROWS[0].rsplit(";", 1)[0] + f";{large_id}\n"
    part2_row = PART2_ROWS[0].rsplit(";", 1)[0] + f";{large_id}\n"
    part1 = _write(tmp_path / "part1.csv", PART1_HEADER, [part1_row, PART1_ROWS[1]])
    part2 = _write(tmp_path / "part2.csv", PART2_HEADER, [part2_row, PART2_ROWS[1]])

    with pytest.raises(DataContractError, match="Invalid id values"):
        load_training_data(part1, part2)


def test_literal_na_and_empty_cell_are_conflicting_raw_rows(tmp_path: Path) -> None:
    empty_ras = "160;iii;www;80.0;iii;5.0;eee;800000.0;xxx;;0\n"
    literal_na = "160;iii;www;80.0;iii;5.0;eee;800000.0;xxx;NA;0\n"
    part1 = _write(
        tmp_path / "part1.csv", PART1_HEADER, [empty_ras, literal_na, PART1_ROWS[1]]
    )
    part2 = _write(tmp_path / "part2.csv", PART2_HEADER, PART2_ROWS)

    with pytest.raises(DataContractError, match=r"conflicting rows for ids \[0\]"):
        load_training_data(part1, part2)


@pytest.mark.parametrize(
    "target_rows",
    [
        [PART2_ROWS[0], PART2_ROWS[1].replace(";y;", ";n;")],
        [PART2_ROWS[0], PART2_ROWS[1].replace(";y;", ";maybe;")],
        [PART2_ROWS[0], PART2_ROWS[1].replace(";y;", ";;")],
    ],
)
def test_target_must_contain_exactly_both_declared_labels(
    tmp_path: Path, target_rows: list[str]
) -> None:
    part1 = _write(tmp_path / "part1.csv", PART1_HEADER, PART1_ROWS)
    part2 = _write(tmp_path / "part2.csv", PART2_HEADER, target_rows)

    with pytest.raises(DataContractError, match="Class must contain exactly"):
        load_training_data(part1, part2)


def test_unexpected_or_reordered_columns_are_rejected(tmp_path: Path) -> None:
    part1 = _write(
        tmp_path / "part1.csv", PART1_HEADER.replace("BIB;COD", "COD;BIB"), PART1_ROWS
    )
    part2 = _write(tmp_path / "part2.csv", PART2_HEADER, PART2_ROWS)

    with pytest.raises(DataContractError, match="Unexpected columns"):
        load_training_data(part1, part2)


def test_reference_files_have_expected_entity_contract() -> None:
    input_dir_value = os.environ.get("SIEMENS_TASK4_INPUT_DIR")
    if input_dir_value is None:
        pytest.skip("Set SIEMENS_TASK4_INPUT_DIR to validate private assignment data")
    input_dir = Path(input_dir_value)

    dataset = load_training_data(
        input_dir / "Training_part1.csv", input_dir / "Training_part2.csv"
    )

    assert dataset.audit.part1_rows == 4_070
    assert dataset.audit.part2_rows == 4_070
    assert dataset.audit.part1_exact_duplicates == 370
    assert dataset.audit.part2_exact_duplicates == 370
    assert dataset.audit.naive_join_rows == 4_475
    assert dataset.audit.entity_rows == 3_700
    assert dataset.frame["id"].is_unique
    assert isinstance(dataset.frame, pd.DataFrame)
