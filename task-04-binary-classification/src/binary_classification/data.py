"""Validated ingestion for the two entity-level training tables."""

import csv
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

PART1_COLUMNS = (
    "BIB",
    "COD",
    "ERG",
    "FAN",
    "GJAH",
    "LUK",
    "MYR",
    "NUS",
    "PKD",
    "RAS",
    "id",
)
PART2_COLUMNS = ("SIS", "TOK", "UIN", "VOL", "WET", "KAT", "XIN", "Class", "id")
TARGET_VALUES = frozenset({"n", "y"})


class DataContractError(ValueError):
    """Raised when input data cannot represent one row per entity."""


@dataclass(frozen=True, slots=True)
class JoinAudit:
    """Counts that make the duplicate-driven join trap visible to reviewers."""

    part1_rows: int
    part2_rows: int
    part1_exact_duplicates: int
    part2_exact_duplicates: int
    naive_join_rows: int
    entity_rows: int


@dataclass(frozen=True, slots=True)
class TrainingDataset:
    frame: pd.DataFrame
    audit: JoinAudit


def _read_table(path: str | Path, expected_columns: tuple[str, ...]) -> pd.DataFrame:
    try:
        with Path(path).open(encoding="utf-8", newline="") as source:
            rows = list(csv.reader(source, delimiter=";", strict=True))
    except (OSError, UnicodeError, csv.Error) as error:
        raise DataContractError(f"Could not read {path}: {error}") from error

    if not rows or tuple(rows[0]) != expected_columns:
        actual_columns = tuple(rows[0]) if rows else ()
        raise DataContractError(
            f"Unexpected columns in {path}: expected {expected_columns}, "
            f"got {actual_columns}"
        )
    invalid_widths = [
        row_number
        for row_number, row in enumerate(rows[1:], start=2)
        if len(row) != len(expected_columns)
    ]
    if invalid_widths:
        raise DataContractError(
            f"Unexpected field count in {path} at rows {invalid_widths[:5]}"
        )

    frame = pd.DataFrame(rows[1:], columns=expected_columns, dtype=object)

    parsed_ids: list[int] = []
    invalid_ids: list[int] = []
    for position, raw_value in enumerate(frame["id"].tolist()):
        value = str(raw_value)
        is_decimal_integer = bool(value) and value.lstrip("+-").isdigit()
        parsed = int(value) if is_decimal_integer else None
        if parsed is None or not -(2**63) <= parsed < 2**63:
            invalid_ids.append(position)
        else:
            parsed_ids.append(parsed)
    if invalid_ids:
        raise DataContractError(
            f"Invalid id values in {path} at rows {invalid_ids[:5]}"
        )

    result = frame.copy()
    result["id"] = pd.Series(parsed_ids, dtype="int64")
    return result


def _deduplicate_entities(
    frame: pd.DataFrame, source_name: str
) -> tuple[pd.DataFrame, int]:
    exact_duplicates = int(frame.duplicated(keep="first").sum())
    entities = frame.drop_duplicates(ignore_index=True)
    conflicts = entities[entities.duplicated("id", keep=False)]
    if not conflicts.empty:
        conflict_ids = sorted(int(value) for value in conflicts["id"].unique())[:5]
        raise DataContractError(
            f"{source_name} contains conflicting rows for ids {conflict_ids}"
        )
    return entities, exact_duplicates


def load_training_data(
    part1_path: str | Path, part2_path: str | Path
) -> TrainingDataset:
    """Load, validate, deduplicate, and one-to-one join both assignment tables."""

    part1 = _read_table(part1_path, PART1_COLUMNS)
    part2 = _read_table(part2_path, PART2_COLUMNS)
    part1_entities, part1_duplicates = _deduplicate_entities(part1, "part1")
    part2_entities, part2_duplicates = _deduplicate_entities(part2, "part2")

    # Normalize missing cells only after raw-row conflict detection distinguishes them
    # from literal category values such as "NA".
    part1_entities = part1_entities.replace("", pd.NA)
    part2_entities = part2_entities.replace("", pd.NA)

    target_values = frozenset(part2_entities["Class"].dropna().unique())
    if part2_entities["Class"].isna().any() or target_values != TARGET_VALUES:
        raise DataContractError(
            f"Class must contain exactly {sorted(TARGET_VALUES)}, got "
            f"{sorted(str(value) for value in target_values)}"
        )

    part1_ids = set(part1_entities["id"])
    part2_ids = set(part2_entities["id"])
    if part1_ids != part2_ids:
        missing_from_part1 = sorted(part2_ids - part1_ids)[:5]
        missing_from_part2 = sorted(part1_ids - part2_ids)[:5]
        raise DataContractError(
            "Input tables contain different entities: "
            f"missing from part1={missing_from_part1}, "
            f"missing from part2={missing_from_part2}"
        )

    # The raw count is retained as evidence of the many-to-many multiplication trap.
    naive_join_rows = len(part1.merge(part2, on="id", validate="many_to_many"))
    joined = part1_entities.merge(
        part2_entities, on="id", validate="one_to_one", sort=False
    )
    audit = JoinAudit(
        part1_rows=len(part1),
        part2_rows=len(part2),
        part1_exact_duplicates=part1_duplicates,
        part2_exact_duplicates=part2_duplicates,
        naive_join_rows=naive_join_rows,
        entity_rows=len(joined),
    )
    return TrainingDataset(frame=joined, audit=audit)
