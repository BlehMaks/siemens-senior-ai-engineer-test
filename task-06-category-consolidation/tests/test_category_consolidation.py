from __future__ import annotations

import tracemalloc
from math import inf, nan, nextafter
from time import perf_counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from category_consolidation import (
    MISSING_CATEGORY,
    RareCategoryConsolidator,
    UnhashableCategoryError,
    consolidate_rare_categories,
)


class _SemanticCategory:
    def __init__(self, value: str) -> None:
        self.value = value

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _SemanticCategory) and self.value == other.value


class _UnexpectedHashFailure:
    def __hash__(self) -> int:
        raise RuntimeError("unexpected hash failure")


def test_strict_less_than_boundary_and_order_are_preserved() -> None:
    values = ["a", "a", "b", "b", "c"]

    assert consolidate_rare_categories(values, 40.0) == [
        "a",
        "a",
        "b",
        "b",
        "__RARE__",
    ]


def test_exact_fractional_boundary_is_retained() -> None:
    values = ["boundary", "major", "major", "major", "major", "major"]

    assert consolidate_rare_categories(values, 100.0 / 6.0)[0] == "boundary"


def test_equivalent_high_fraction_boundary_is_retained() -> None:
    values = ["boundary"] * 5 + ["other"]

    assert (
        consolidate_rare_categories(values, (5.0 / 6.0) * 100.0)[:5] == ["boundary"] * 5
    )


def test_next_float_above_fractional_boundary_is_consolidated() -> None:
    values = ["boundary"] * 5 + ["other"]
    threshold = nextafter((5.0 / 6.0) * 100.0, inf)

    assert consolidate_rare_categories(values, threshold)[:5] == ["__RARE__"] * 5


def test_next_float_above_half_boundary_is_consolidated() -> None:
    threshold = nextafter(50.0, inf)

    assert consolidate_rare_categories(["a", "b"], threshold) == [
        "__RARE__",
        "__RARE__",
    ]


def test_empty_input_returns_empty_output() -> None:
    consolidator = RareCategoryConsolidator(25.0)

    assert consolidator.fit_transform([]).values == []
    assert consolidator.transform([]) == []


def test_transform_before_fit_fails() -> None:
    with pytest.raises(RuntimeError, match="fit"):
        RareCategoryConsolidator(25.0).transform(["value"])


def test_zero_threshold_keeps_seen_categories() -> None:
    values = ["a", "b", "c"]

    assert consolidate_rare_categories(values, 0.0) == values


def test_hundred_threshold_keeps_only_full_dataset_category() -> None:
    assert consolidate_rare_categories(["a", "a"], 100.0) == ["a", "a"]
    assert consolidate_rare_categories(["a", "b"], 100.0) == ["__RARE__", "__RARE__"]


def test_missing_sentinel_counts_toward_denominator_and_exact_boundary() -> None:
    values = [None, None, "blue", "green"]

    assert consolidate_rare_categories(values, 50.0, missing_sentinel=None) == [
        None,
        None,
        "__RARE__",
        "__RARE__",
    ]


def test_collision_safe_fallback_label_is_resolved_at_fit_time() -> None:
    consolidator = RareCategoryConsolidator(50.0, rare_label="__RARE__")

    result = consolidator.fit_transform(["__RARE__", "x", "y"])

    assert consolidator.resolved_rare_label == "__RARE____rare"
    assert result.values == ["__RARE____rare", "__RARE____rare", "__RARE____rare"]


def test_collision_fallback_is_deterministic_for_equal_objects() -> None:
    first = RareCategoryConsolidator(
        50.0, rare_label=_SemanticCategory("requested")
    ).fit([_SemanticCategory("requested"), "other"])
    second = RareCategoryConsolidator(
        50.0, rare_label=_SemanticCategory("requested")
    ).fit([_SemanticCategory("requested"), "other"])

    assert first.resolved_rare_label == second.resolved_rare_label


def test_unseen_categories_map_to_rare_label_with_diagnostics() -> None:
    consolidator = RareCategoryConsolidator(50.0)
    consolidator.fit(["red", "red", "blue", "green"])

    result = consolidator.transform_with_diagnostics(["red", "yellow", "blue", "cyan"])

    assert result.values == ["red", "__RARE__", "__RARE__", "__RARE__"]
    assert result.diagnostics.unseen_indexes == (1, 3)
    assert result.diagnostics.unseen_values == ("yellow", "cyan")


def test_fit_mapping_is_reused_for_later_transforms() -> None:
    consolidator = RareCategoryConsolidator(34.0)
    first = consolidator.fit_transform(["a", "a", "b"])
    second = consolidator.transform(["b", "a", "c"])

    assert first.values == ["a", "a", "__RARE__"]
    assert second == ["__RARE__", "a", "__RARE__"]


@pytest.mark.parametrize("invalid_threshold", [-0.1, 100.1, nan, inf, "20"])
def test_invalid_thresholds_fail(invalid_threshold: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RareCategoryConsolidator(invalid_threshold)  # type: ignore[arg-type]


@pytest.mark.parametrize("boolean_threshold", [False, True])
def test_boolean_thresholds_are_rejected(boolean_threshold: bool) -> None:
    with pytest.raises(TypeError, match="finite real number"):
        RareCategoryConsolidator(boolean_threshold)


def test_unhashable_values_raise_indexed_error() -> None:
    with pytest.raises(UnhashableCategoryError, match="index 1") as exc_info:
        consolidate_rare_categories(["a", ["b"]], 20.0)

    assert exc_info.value.index == 1


def test_nested_unhashable_value_raises_indexed_error() -> None:
    with pytest.raises(UnhashableCategoryError) as exc_info:
        consolidate_rare_categories(["a", (["nested"],)], 20.0)

    assert exc_info.value.index == 1


def test_writable_memoryview_raises_indexed_error() -> None:
    with pytest.raises(UnhashableCategoryError) as exc_info:
        consolidate_rare_categories(["a", memoryview(bytearray(b"value"))], 20.0)

    assert exc_info.value.index == 1


def test_unexpected_hash_exception_is_not_hidden() -> None:
    with pytest.raises(RuntimeError, match="unexpected hash failure"):
        consolidate_rare_categories([_UnexpectedHashFailure()], 20.0)


def test_custom_missing_sentinel_can_be_retained_when_frequent() -> None:
    values = [MISSING_CATEGORY, MISSING_CATEGORY, "ok"]
    consolidator = RareCategoryConsolidator(50.0, missing_sentinel=MISSING_CATEGORY)

    result = consolidator.fit_transform(values)

    assert result.values == [MISSING_CATEGORY, MISSING_CATEGORY, "__RARE__"]


@settings(deadline=None, max_examples=80)
@given(
    values=st.lists(
        st.one_of(
            st.text(min_size=1, max_size=4), st.integers(min_value=0, max_value=4)
        ),
        min_size=0,
        max_size=40,
    ),
    threshold=st.floats(
        min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False
    ),
)
def test_property_matches_reference_implementation(
    values: list[str | int],
    threshold: float,
) -> None:
    result = consolidate_rare_categories(values, threshold)
    expected = _reference_consolidation(values, threshold)

    assert result == expected
    assert len(result) == len(values)


def test_fit_and_transform_perf_budget() -> None:
    values = [f"cat-{index % 20}" for index in range(100_000)]
    consolidator = RareCategoryConsolidator(3.0)

    tracemalloc.start()
    start = perf_counter()
    result = consolidator.fit_transform(values)
    elapsed = perf_counter() - start
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(result.values) == len(values)
    assert elapsed < 1.0
    assert peak_bytes < 256 * 1024 * 1024


def _reference_consolidation(
    values: list[str | int], threshold: float
) -> list[str | int]:
    if not values:
        return []

    total = len(values)
    counts: dict[str | int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1

    output: list[str | int] = []
    for value in values:
        percent = (counts[value] / total) * 100.0
        output.append(value if percent >= threshold else "__RARE__")
    return output
