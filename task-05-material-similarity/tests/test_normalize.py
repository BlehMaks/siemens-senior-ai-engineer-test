from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from material_similarity.normalize import normalize_description


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Fuse  ABC-123/V2,  6.3 Amps  ", "fuse abc-123/v2, 6.3a"),
        (
            "250 Volts AC; 5 \N{MULTIPLICATION SIGN} 20 millimetres",
            "250vac; 5x20mm",
        ),
        ("5 mm \N{MULTIPLICATION SIGN} 20 mm", "5mmx20mm"),
        ("Response 5 mS; delay 5 ms", "response 5mS; delay 5ms"),
        ("6.9(Typ)W / -55 Cel to 125 °C", "6.9(typ)w / -55cel to 125cel"),
        ("FRN-R-10, AC/DC, 1/4-inch", "frn-r-10, ac/dc, 1/4-inch"),
        (" \t\n ", ""),
    ],
)
def test_normalize_description_golden_cases(raw: str, expected: str) -> None:
    assert normalize_description(raw) == expected


@given(st.text(max_size=200))
def test_normalization_is_idempotent(description: str) -> None:
    normalized = normalize_description(description)

    assert normalize_description(normalized) == normalized


@given(
    value=st.integers(min_value=0, max_value=1000),
    unit=st.sampled_from(["A", "amp", "Amperes"]),
)
def test_current_rating_variants_keep_their_magnitude(value: int, unit: str) -> None:
    assert normalize_description(f"Fuse {value} {unit}").endswith(f"{value}a")
