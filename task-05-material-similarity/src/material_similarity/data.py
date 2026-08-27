"""Validated loading and deterministic profiling for the fuse catalog."""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

PART_ID_COLUMN = "PART_ID"
DESCRIPTION_COLUMN = "PART_DESCRIPTION"

MATERIAL_COLUMNS = (
    PART_ID_COLUMN,
    DESCRIPTION_COLUMN,
    "Acting",
    "Additional Feature",
    "Application",
    "Blow Characteristic",
    "Body Breadth (mm)",
    "Body Height (mm)",
    "Body Length or Diameter (mm)",
    "Current Rating",
    "Fuse Material",
    "Fuse Size",
    "JESD-609 Code",
    "Joule-integral-Nom (J)",
    "LC Risk",
    "Maximum AC Voltage Rating",
    "Maximum DC Voltage Rating",
    "Maximum Power Dissipation",
    "Mounting",
    "Mounting Feature",
    "Number of Terminals",
    "Operating Temperature-Max (Cel)",
    "Operating Temperature-Min (Cel)",
    "Physical Dimension",
    "Pre-arcing time-Min (ms)",
    "Product Diameter",
    "Product Length",
    "Rated Breaking Capacity (A)",
    "Rated Current (A)",
    "Rated Voltage (V)",
    "Rated Voltage(AC) (V)",
    "Rated Voltage(DC) (V)",
)

# Require a numeric magnitude so ordinary prose containing a single-letter unit is
# not counted as quantitative evidence.
_UNIT_BEARING_VALUE = re.compile(
    r"(?i)(?<![\w.])\d+(?:\.\d+)?(?:\s*\([^)]{1,12}\))?\s*"
    r"(?:mm|millimet(?:er|re)s?|vac|vdc|volts?|v|amperes?|amps?|a|"
    r"watts?|w|milliseconds?|ms|joules?|j|celsius|cel|°c|hz|ohms?)\b"
)


class MaterialDataError(ValueError):
    """Raised when a catalog cannot satisfy the material data boundary."""


@dataclass(frozen=True)
class ColumnProfile:
    """Observed completeness and value-shape evidence for one source column."""

    name: str
    blank_count: int
    unique_nonempty_count: int
    unit_bearing_count: int


@dataclass(frozen=True)
class MaterialProfile:
    """Deterministic catalog facts used by the analysis and later retrieval work."""

    row_count: int
    column_count: int
    unique_part_id_count: int
    blank_description_count: int
    duplicate_description_count: int
    duplicate_description_group_count: int
    columns: tuple[ColumnProfile, ...]

    @property
    def sparse_columns(self) -> tuple[str, ...]:
        """Return fields that are blank for at least half of the catalog."""

        return tuple(
            column.name
            for column in self.columns
            if column.blank_count * 2 >= self.row_count
        )

    def column(self, name: str) -> ColumnProfile:
        """Return the profile for ``name`` or fail clearly for an unknown field."""

        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)


def load_materials(path: Path) -> tuple[dict[str, str], ...]:
    """Load the semicolon-delimited catalog while keeping source blanks explicit."""

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        _validate_schema(reader.fieldnames or ())
        rows = tuple(_material_row(raw) for raw in reader)

    if not rows:
        raise MaterialDataError("material catalog is empty")
    part_ids = [row[PART_ID_COLUMN].strip() for row in rows]
    if any(not part_id for part_id in part_ids):
        raise MaterialDataError("PART_ID contains blank values")
    duplicates = sorted(
        part_id for part_id, count in Counter(part_ids).items() if count > 1
    )
    if duplicates:
        raise MaterialDataError(f"PART_ID contains duplicates: {duplicates}")

    return tuple(
        {**row, PART_ID_COLUMN: part_id}
        for row, part_id in zip(rows, part_ids, strict=True)
    )


def profile_materials(materials: Sequence[Mapping[str, str]]) -> MaterialProfile:
    """Profile blanks, cardinality, duplicates, and unit-bearing values."""

    if not materials:
        raise MaterialDataError("material catalog is empty")
    for material in materials:
        _validate_schema(tuple(material))

    part_ids = _clean_values(materials, PART_ID_COLUMN)
    if any(not part_id for part_id in part_ids):
        raise MaterialDataError("PART_ID contains blank values")
    if len(set(part_ids)) != len(part_ids):
        raise MaterialDataError("PART_ID contains duplicates")

    descriptions = _clean_values(materials, DESCRIPTION_COLUMN)
    description_counts = Counter(value for value in descriptions if value)
    duplicate_groups = [count for count in description_counts.values() if count > 1]

    columns = tuple(
        _profile_column(name, _clean_values(materials, name))
        for name in MATERIAL_COLUMNS
    )
    return MaterialProfile(
        row_count=len(materials),
        column_count=len(MATERIAL_COLUMNS),
        unique_part_id_count=len(set(part_ids)),
        blank_description_count=sum(not value for value in descriptions),
        # Count repetitions beyond the first row in each identical-text group. This
        # matches the assignment's reported 81 duplicate descriptions.
        duplicate_description_count=sum(count - 1 for count in duplicate_groups),
        duplicate_description_group_count=len(duplicate_groups),
        columns=columns,
    )


def _validate_schema(columns: Sequence[str]) -> None:
    missing = sorted(set(MATERIAL_COLUMNS) - set(columns))
    unexpected = sorted(set(columns) - set(MATERIAL_COLUMNS))
    if missing or unexpected or len(columns) != len(MATERIAL_COLUMNS):
        raise MaterialDataError(
            f"unexpected material schema; missing={missing}, unexpected={unexpected}"
        )


def _material_row(raw: Mapping[str | None, str | None]) -> dict[str, str]:
    if None in raw or any(value is None for value in raw.values()):
        raise MaterialDataError("material row does not match the declared schema")
    return {name: cast(str, raw[name]) for name in MATERIAL_COLUMNS}


def _clean_values(materials: Sequence[Mapping[str, str]], column: str) -> list[str]:
    return [material[column].strip() for material in materials]


def _profile_column(name: str, values: list[str]) -> ColumnProfile:
    nonempty = [value for value in values if value]
    return ColumnProfile(
        name=name,
        blank_count=len(values) - len(nonempty),
        unique_nonempty_count=len(set(nonempty)),
        unit_bearing_count=sum(
            bool(_UNIT_BEARING_VALUE.search(value)) for value in nonempty
        ),
    )
