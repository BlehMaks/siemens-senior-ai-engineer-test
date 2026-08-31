"""Validation and application of explicit category aliases."""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from typing import Any, cast

from .core import UnhashableCategoryError


def validate_alias_maps(
    alias_maps: Mapping[str, Mapping[Any, Any]] | None,
    *,
    selected_columns: Sequence[str],
    observed_by_column: Mapping[str, Sequence[object]],
    rare_label: Hashable,
    missing_sentinel: Hashable,
) -> dict[str, dict[Hashable, Hashable]]:
    """Validate and freeze flat, explicit alias-to-canonical maps."""
    if alias_maps is None:
        return {}
    if not isinstance(alias_maps, Mapping):
        raise TypeError("alias_maps must be a mapping by selected column")

    if not all(isinstance(column, str) for column in alias_maps):
        raise TypeError("alias map column names must be strings")
    unknown_columns = sorted(set(alias_maps) - set(selected_columns))
    if unknown_columns:
        raise ValueError(f"alias map columns are not selected: {unknown_columns}")

    frozen: dict[str, dict[Hashable, Hashable]] = {}
    for column, policy in alias_maps.items():
        if not isinstance(policy, Mapping):
            raise TypeError(f"alias map for {column!r} must be a mapping")
        if not policy:
            continue

        raw_aliases = dict(policy)
        _validate_hashable_policy(column, raw_aliases)
        aliases = {
            alias: cast(Hashable, canonical) for alias, canonical in raw_aliases.items()
        }
        _reject_reserved_values(column, aliases, rare_label, missing_sentinel)
        _reject_cycles(column, aliases)

        chained_targets = set(aliases.values()) & set(aliases)
        if chained_targets:
            raise ValueError(
                f"alias map for {column!r} has alias/canonical collisions: "
                f"{sorted(chained_targets, key=repr)!r}"
            )

        observed = _observed_set(observed_by_column[column])
        unknown_targets = set(aliases.values()) - observed
        if unknown_targets:
            raise ValueError(
                f"alias map for {column!r} has unknown canonical targets: "
                f"{sorted(unknown_targets, key=repr)!r}"
            )
        frozen[column] = aliases
    return frozen


def apply_aliases(
    values: Sequence[object],
    aliases: Mapping[Hashable, Hashable],
) -> Sequence[object]:
    """Apply one already-validated exact alias map."""
    if not aliases:
        return values
    return [aliases.get(value, value) for value in values]


def _validate_hashable_policy(
    column: str,
    aliases: Mapping[Any, Any],
) -> None:
    for alias, canonical in aliases.items():
        for label, value in (("alias", alias), ("canonical target", canonical)):
            try:
                hash(value)
            except (TypeError, ValueError):
                raise TypeError(
                    f"{label} in alias map for {column!r} must be hashable"
                ) from None


def _reject_reserved_values(
    column: str,
    aliases: Mapping[Hashable, Hashable],
    rare_label: Hashable,
    missing_sentinel: Hashable,
) -> None:
    reserved = {rare_label, missing_sentinel}
    collisions = (set(aliases) | set(aliases.values())) & reserved
    if collisions:
        raise ValueError(
            f"alias map for {column!r} uses reserved labels: "
            f"{sorted(collisions, key=repr)!r}"
        )


def _reject_cycles(
    column: str,
    aliases: Mapping[Hashable, Hashable],
) -> None:
    for start in aliases:
        seen: set[Hashable] = set()
        current = start
        while current in aliases:
            if current in seen:
                raise ValueError(f"alias map for {column!r} contains a cycle")
            seen.add(current)
            current = aliases[current]


def _observed_set(values: Sequence[object]) -> set[Hashable]:
    observed: set[Hashable] = set()
    for index, value in enumerate(values):
        try:
            observed.add(value)
        except (TypeError, ValueError):
            raise UnhashableCategoryError(index=index, value=value) from None
    return observed
