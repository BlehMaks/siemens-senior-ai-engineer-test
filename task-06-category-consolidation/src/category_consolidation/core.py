from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from math import inf, isfinite, nextafter
from numbers import Real


class _MissingCategorySentinel:
    def __repr__(self) -> str:
        return "MISSING_CATEGORY"


MISSING_CATEGORY = _MissingCategorySentinel()


class UnhashableCategoryError(TypeError):
    def __init__(self, index: int, value: object) -> None:
        self.index = index
        self.value = value
        super().__init__(
            f"category at index {index} is not hashable: {type(value).__name__}"
        )


@dataclass(frozen=True)
class TransformDiagnostics:
    unseen_indexes: tuple[int, ...] = ()
    unseen_values: tuple[Hashable, ...] = ()


@dataclass(frozen=True)
class TransformResult:
    values: list[Hashable]
    diagnostics: TransformDiagnostics = field(default_factory=TransformDiagnostics)


@dataclass(slots=True)
class RareCategoryConsolidator:
    threshold_percent: float
    rare_label: Hashable = "__RARE__"
    missing_sentinel: Hashable = MISSING_CATEGORY
    observed_categories: frozenset[Hashable] = field(
        init=False, default_factory=frozenset
    )
    retained_categories: frozenset[Hashable] = field(
        init=False, default_factory=frozenset
    )
    resolved_rare_label: Hashable = field(init=False)
    _is_fitted: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        self.threshold_percent = _validate_threshold(self.threshold_percent)
        _assert_hashable(self.rare_label, label="rare_label")
        _assert_hashable(self.missing_sentinel, label="missing_sentinel")
        self.resolved_rare_label = self.rare_label

    def fit(self, values: Iterable[object]) -> RareCategoryConsolidator:
        validated_values = _validate_values(values)
        resolved_rare_label = _resolve_rare_label(
            requested_label=self.rare_label,
            observed_values=validated_values,
            missing_sentinel=self.missing_sentinel,
        )
        observed_categories = frozenset(validated_values)
        retained_categories = frozenset(
            value
            for value, count in Counter(validated_values).items()
            if _meets_threshold(
                count=count,
                total=len(validated_values),
                threshold_percent=self.threshold_percent,
            )
        )
        self.resolved_rare_label = resolved_rare_label
        self.observed_categories = observed_categories
        self.retained_categories = retained_categories
        self._is_fitted = True
        return self

    def transform(self, values: Iterable[object]) -> list[Hashable]:
        return self.transform_with_diagnostics(values).values

    def transform_with_diagnostics(self, values: Iterable[object]) -> TransformResult:
        # Empty learned sets are valid, so fitted state cannot be inferred from them.
        if not self._is_fitted:
            raise RuntimeError("fit must be called before transform")

        validated_values = _validate_values(values)
        unseen_indexes: list[int] = []
        unseen_values: list[Hashable] = []
        transformed: list[Hashable] = []

        for index, value in enumerate(validated_values):
            if value not in self.observed_categories:
                unseen_indexes.append(index)
                unseen_values.append(value)
                transformed.append(self.resolved_rare_label)
                continue

            if value in self.retained_categories:
                transformed.append(value)
                continue

            transformed.append(self.resolved_rare_label)

        return TransformResult(
            values=transformed,
            diagnostics=TransformDiagnostics(
                unseen_indexes=tuple(unseen_indexes),
                unseen_values=tuple(unseen_values),
            ),
        )

    def fit_transform(self, values: Iterable[object]) -> TransformResult:
        validated_values = _validate_values(values)
        self.fit(validated_values)
        return self.transform_with_diagnostics(validated_values)


def consolidate_rare_categories(
    values: Iterable[object],
    threshold_percent: float,
    *,
    rare_label: Hashable = "__RARE__",
    missing_sentinel: Hashable = MISSING_CATEGORY,
) -> list[Hashable]:
    return (
        RareCategoryConsolidator(
            threshold_percent=threshold_percent,
            rare_label=rare_label,
            missing_sentinel=missing_sentinel,
        )
        .fit_transform(values)
        .values
    )


def _validate_threshold(threshold_percent: float) -> float:
    if isinstance(threshold_percent, bool) or not isinstance(threshold_percent, Real):
        raise TypeError("threshold_percent must be a finite real number from 0 to 100")

    threshold = float(threshold_percent)
    if not isfinite(threshold) or not 0.0 <= threshold <= 100.0:
        raise ValueError("threshold_percent must be a finite real number from 0 to 100")
    return threshold


def _validate_values(values: Iterable[object]) -> tuple[Hashable, ...]:
    validated: list[Hashable] = []
    for index, value in enumerate(values):
        if not isinstance(value, Hashable):
            raise UnhashableCategoryError(index=index, value=value)
        try:
            # Hashable misses nested mutables and writable memoryviews until hash().
            hash(value)
        except (TypeError, ValueError):
            raise UnhashableCategoryError(index=index, value=value) from None
        validated.append(value)
    return tuple(validated)


def _assert_hashable(value: object, *, label: str) -> None:
    if not isinstance(value, Hashable):
        raise TypeError(f"{label} must be hashable")
    try:
        hash(value)
    except (TypeError, ValueError):
        raise TypeError(f"{label} must be hashable") from None


def _meets_threshold(*, count: int, total: int, threshold_percent: float) -> bool:
    frequency = count / total
    threshold = threshold_percent / 100.0
    # Percentage scaling can move an equal rational boundary by one float step.
    return frequency >= nextafter(threshold, -inf)


def _resolve_rare_label(
    *,
    requested_label: Hashable,
    observed_values: tuple[Hashable, ...],
    missing_sentinel: Hashable,
) -> Hashable:
    collisions = set(observed_values)
    if requested_label not in collisions and requested_label != missing_sentinel:
        return requested_label

    # A fixed base avoids object representations whose default text contains addresses.
    base_label = "__RARE____rare"
    candidate = base_label
    suffix = 1

    # Fit-time label resolution prevents a real category from silently disappearing.
    while candidate in collisions or candidate == missing_sentinel:
        candidate = f"{base_label}_{suffix}"
        suffix += 1

    return candidate
