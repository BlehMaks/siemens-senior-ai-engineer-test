"""Safe, versioned JSON artifacts for fitted category mappings."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Hashable, Mapping
from math import isfinite
from typing import Any

import numpy as np
from sklearn.utils.validation import check_is_fitted  # type: ignore[import-untyped]

from .aliases import validate_alias_maps
from .core import MISSING_CATEGORY, RareCategoryConsolidator
from .sklearn import CategoryConsolidationTransformer

SCHEMA_VERSION = 1
ALIAS_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_VERSION, ALIAS_SCHEMA_VERSION}


class ArtifactValidationError(ValueError):
    """Raised when a mapping artifact is malformed or unsupported."""


def encode_scalar(value: Hashable) -> dict[str, object]:
    """Encode one explicitly supported category scalar as JSON data."""
    if value is MISSING_CATEGORY:
        return {"type": "missing_category"}
    if value is None:
        return {"type": "none"}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        return {"type": "int", "value": str(value)}
    if type(value) is float:
        if value != value:
            encoded = "nan"
        elif value == float("inf"):
            encoded = "inf"
        elif value == float("-inf"):
            encoded = "-inf"
        else:
            encoded = repr(value)
        return {"type": "float", "value": encoded}
    if type(value) is str:
        return {"type": "str", "value": value}
    raise TypeError(
        "mapping artifacts support only None, bool, int, float, str, and "
        "MISSING_CATEGORY"
    )


def decode_scalar(value: object) -> Hashable:
    """Decode a scalar produced by :func:`encode_scalar`."""
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ArtifactValidationError("encoded scalar must be an object with a type")
    scalar_type = value["type"]
    expected_keys = (
        {"type"}
        if scalar_type in {"none", "missing_category"}
        else {
            "type",
            "value",
        }
    )
    _require_keys(value, expected_keys, "encoded scalar")

    if scalar_type == "missing_category":
        return MISSING_CATEGORY
    if scalar_type == "none":
        return None
    raw = value["value"]
    if scalar_type == "bool" and type(raw) is bool:
        return raw
    if scalar_type == "int" and isinstance(raw, str):
        try:
            decoded_int = int(raw)
        except ValueError as error:
            raise ArtifactValidationError("invalid encoded integer") from error
        if str(decoded_int) != raw:
            raise ArtifactValidationError("encoded integer must be canonical")
        return decoded_int
    if scalar_type == "float" and isinstance(raw, str):
        if raw == "nan":
            return float("nan")
        if raw == "inf":
            return float("inf")
        if raw == "-inf":
            return float("-inf")
        try:
            decoded_float = float(raw)
        except ValueError as error:
            raise ArtifactValidationError("invalid encoded float") from error
        if not isfinite(decoded_float) or repr(decoded_float) != raw:
            raise ArtifactValidationError("encoded float must be finite and canonical")
        return decoded_float
    if scalar_type == "str" and isinstance(raw, str):
        return raw
    raise ArtifactValidationError(f"invalid or unsupported scalar type: {scalar_type}")


def dump_mapping_artifact(transformer: CategoryConsolidationTransformer) -> str:
    """Serialize a fitted transformer to deterministic JSON."""
    check_is_fitted(transformer, attributes=["consolidators_", "feature_names_in_"])
    mappings: dict[str, object] = {}
    for column in transformer.selected_columns_:
        consolidator = transformer.consolidators_[column]
        mappings[column] = {
            "observed_categories": _encode_sorted(consolidator.observed_categories),
            "retained_categories": _encode_sorted(consolidator.retained_categories),
            "resolved_rare_label": encode_scalar(consolidator.resolved_rare_label),
        }

    config: dict[str, object] = {
        "columns": list(transformer.selected_columns_),
        "feature_names": transformer.feature_names_in_.tolist(),
        "threshold_percent": transformer.threshold_percent,
        "min_count": transformer.min_count,
        "rare_label": encode_scalar(transformer.rare_label),
        "missing_sentinel": encode_scalar(transformer.missing_sentinel),
        "mappings": mappings,
    }
    schema_version = SCHEMA_VERSION
    if transformer.alias_maps_:
        schema_version = ALIAS_SCHEMA_VERSION
        config["alias_maps"] = _encode_alias_maps(transformer.alias_maps_)

    body: dict[str, object] = {
        "schema_version": schema_version,
        "transformer": config,
    }
    document = {**body, "fingerprint": _fingerprint(body)}
    return json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"


def load_mapping_artifact(payload: str) -> CategoryConsolidationTransformer:
    """Validate JSON and rebuild a fitted transformer without executing code."""
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda constant: _raise_invalid_constant(constant),
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise ArtifactValidationError("artifact is not valid strict JSON") from error
    if not isinstance(document, dict):
        raise ArtifactValidationError("artifact root must be an object")
    _require_keys(
        document, {"schema_version", "transformer", "fingerprint"}, "artifact"
    )
    schema_version = document["schema_version"]
    if (
        type(schema_version) is not int
        or schema_version not in SUPPORTED_SCHEMA_VERSIONS
    ):
        raise ArtifactValidationError(
            f"unsupported artifact schema_version: {schema_version!r}"
        )
    if not isinstance(document["fingerprint"], str):
        raise ArtifactValidationError("artifact fingerprint must be a string")
    body = {
        "schema_version": document["schema_version"],
        "transformer": document["transformer"],
    }
    expected_fingerprint = _fingerprint(body)
    if not hmac.compare_digest(document["fingerprint"], expected_fingerprint):
        raise ArtifactValidationError("artifact fingerprint mismatch")

    config = document["transformer"]
    if not isinstance(config, dict):
        raise ArtifactValidationError("transformer must be an object")
    expected_config_keys = {
        "columns",
        "feature_names",
        "threshold_percent",
        "min_count",
        "rare_label",
        "missing_sentinel",
        "mappings",
    }
    if schema_version == ALIAS_SCHEMA_VERSION:
        expected_config_keys.add("alias_maps")
    _require_keys(config, expected_config_keys, "transformer")
    columns = _validate_names(config["columns"], "columns", allow_empty=False)
    feature_names = _validate_names(
        config["feature_names"], "feature_names", allow_empty=False
    )
    if not set(columns).issubset(feature_names):
        raise ArtifactValidationError("selected columns must exist in feature_names")
    mappings = config["mappings"]
    if not isinstance(mappings, dict) or set(mappings) != set(columns):
        raise ArtifactValidationError("mappings must exactly match selected columns")

    rare_label = decode_scalar(config["rare_label"])
    missing_sentinel = decode_scalar(config["missing_sentinel"])
    try:
        alias_maps = (
            _decode_alias_maps(config["alias_maps"])
            if schema_version == ALIAS_SCHEMA_VERSION
            else {}
        )
        transformer = CategoryConsolidationTransformer(
            columns=columns,
            threshold_percent=config["threshold_percent"],
            min_count=config["min_count"],
            rare_label=rare_label,
            missing_sentinel=missing_sentinel,
            alias_maps=alias_maps or None,
        )
        consolidators = {
            column: _load_consolidator(
                mappings[column],
                threshold_percent=transformer.threshold_percent,
                min_count=transformer.min_count,
                rare_label=rare_label,
                missing_sentinel=missing_sentinel,
            )
            for column in columns
        }
        alias_maps = validate_alias_maps(
            alias_maps,
            selected_columns=columns,
            observed_by_column={
                column: tuple(consolidators[column].observed_categories)
                for column in columns
            },
            rare_label=rare_label,
            missing_sentinel=missing_sentinel,
        )
        if schema_version == ALIAS_SCHEMA_VERSION and not alias_maps:
            raise ArtifactValidationError(
                "schema version 2 requires at least one alias mapping"
            )
    except ArtifactValidationError:
        raise
    except (TypeError, ValueError) as error:
        raise ArtifactValidationError(
            "artifact contains invalid transformer state"
        ) from error

    transformer.selected_columns_ = columns
    transformer.feature_names_in_ = np.asarray(feature_names, dtype=object)
    transformer.n_features_in_ = len(feature_names)
    transformer.consolidators_ = consolidators
    transformer.alias_maps_ = alias_maps
    return transformer


def artifact_fingerprint(payload: str) -> str:
    """Return the validated fingerprint embedded in an artifact."""
    transformer = load_mapping_artifact(payload)
    del transformer
    document = json.loads(payload)
    return str(document["fingerprint"])


def _load_consolidator(
    value: object,
    *,
    threshold_percent: object,
    min_count: object,
    rare_label: Hashable,
    missing_sentinel: Hashable,
) -> RareCategoryConsolidator:
    if not isinstance(value, dict):
        raise ArtifactValidationError("column mapping must be an object")
    _require_keys(
        value,
        {"observed_categories", "retained_categories", "resolved_rare_label"},
        "column mapping",
    )
    observed = _decode_scalar_list(value["observed_categories"], "observed_categories")
    retained = _decode_scalar_list(value["retained_categories"], "retained_categories")
    if not retained.issubset(observed):
        raise ArtifactValidationError("retained categories must be observed")
    resolved_rare_label = decode_scalar(value["resolved_rare_label"])
    if resolved_rare_label in observed or resolved_rare_label == missing_sentinel:
        raise ArtifactValidationError("resolved rare label collides with a category")

    consolidator = RareCategoryConsolidator(
        threshold_percent=threshold_percent,  # type: ignore[arg-type]
        min_count=min_count,  # type: ignore[arg-type]
        rare_label=rare_label,
        missing_sentinel=missing_sentinel,
    )
    consolidator.observed_categories = observed
    consolidator.retained_categories = retained
    consolidator.resolved_rare_label = resolved_rare_label
    consolidator._is_fitted = True
    return consolidator


def _encode_sorted(values: frozenset[Hashable]) -> list[dict[str, object]]:
    encoded = [encode_scalar(value) for value in values]
    return sorted(encoded, key=_canonical_json)


def _decode_scalar_list(value: object, label: str) -> frozenset[Hashable]:
    if not isinstance(value, list):
        raise ArtifactValidationError(f"{label} must be a list")
    decoded = [decode_scalar(item) for item in value]
    result = frozenset(decoded)
    if len(result) != len(decoded):
        raise ArtifactValidationError(f"{label} must not contain duplicates")
    return result


def _encode_alias_maps(
    alias_maps: Mapping[str, Mapping[Hashable, Hashable]],
) -> dict[str, list[dict[str, object]]]:
    encoded: dict[str, list[dict[str, object]]] = {}
    for column, policy in alias_maps.items():
        entries: list[dict[str, object]] = [
            {"alias": encode_scalar(alias), "canonical": encode_scalar(canonical)}
            for alias, canonical in policy.items()
        ]
        encoded[column] = sorted(entries, key=_canonical_json)
    return encoded


def _decode_alias_maps(
    value: object,
) -> dict[str, dict[Hashable, Hashable]]:
    if not isinstance(value, dict):
        raise ArtifactValidationError("alias_maps must be an object by column")
    decoded: dict[str, dict[Hashable, Hashable]] = {}
    for column, raw_entries in value.items():
        if not isinstance(raw_entries, list):
            raise ArtifactValidationError("alias map entries must be a list")
        aliases: dict[Hashable, Hashable] = {}
        encoded_aliases: set[str] = set()
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                raise ArtifactValidationError("alias map entry must be an object")
            _require_keys(raw_entry, {"alias", "canonical"}, "alias map entry")
            encoded_alias = _canonical_json(raw_entry["alias"])
            if encoded_alias in encoded_aliases:
                raise ArtifactValidationError("alias map must not contain duplicates")
            encoded_aliases.add(encoded_alias)
            alias = decode_scalar(raw_entry["alias"])
            canonical = decode_scalar(raw_entry["canonical"])
            aliases[alias] = canonical
        decoded[column] = aliases
    return decoded


def _validate_names(
    value: object,
    label: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(type(item) is str for item in value):
        raise ArtifactValidationError(f"{label} must be a list of strings")
    if not allow_empty and not value:
        raise ArtifactValidationError(f"{label} must not be empty")
    names = tuple(value)
    if len(set(names)) != len(names):
        raise ArtifactValidationError(f"{label} must be unique")
    return names


def _require_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ArtifactValidationError(
            f"{label} keys must be exactly {sorted(expected)}"
        )


def _fingerprint(body: Mapping[str, object]) -> str:
    digest = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _raise_invalid_constant(constant: str) -> None:
    raise ArtifactValidationError(f"invalid JSON constant: {constant}")
