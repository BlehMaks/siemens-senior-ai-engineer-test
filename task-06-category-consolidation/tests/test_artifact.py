from __future__ import annotations

import json
from decimal import Decimal
from math import isnan
from typing import cast

import pandas as pd
import pytest
from sklearn.utils.validation import NotFittedError  # type: ignore[import-untyped]

from category_consolidation import MISSING_CATEGORY
from category_consolidation.artifact import (
    ArtifactValidationError,
    artifact_fingerprint,
    decode_scalar,
    dump_mapping_artifact,
    encode_scalar,
    load_mapping_artifact,
)
from category_consolidation.sklearn import CategoryConsolidationTransformer


def _fitted_transformer() -> CategoryConsolidationTransformer:
    return CategoryConsolidationTransformer(
        columns=("region", "channel"),
        threshold_percent=30.0,
        min_count=2,
    ).fit(
        pd.DataFrame(
            {
                "region": ["north", "north", "south", None],
                "channel": ["web", "web", "store", "partner"],
                "value": [1, 2, 3, 4],
            }
        )
    )


def test_artifact_round_trip_is_deterministic_and_reproduces_transform() -> None:
    transformer = _fitted_transformer()
    payload = dump_mapping_artifact(transformer)
    restored = load_mapping_artifact(payload)
    inference = pd.DataFrame(
        {
            "region": ["north", "west", None],
            "channel": ["web", "phone", "store"],
            "value": [5, 6, 7],
        }
    )

    assert dump_mapping_artifact(restored) == payload
    assert restored.transform(inference).equals(transformer.transform(inference))
    assert artifact_fingerprint(payload).startswith("sha256:")


def test_empty_alias_policy_preserves_schema_v1_bytes() -> None:
    transformer = _fitted_transformer()
    empty_alias_transformer = CategoryConsolidationTransformer(
        columns=("region", "channel"),
        threshold_percent=30.0,
        min_count=2,
        alias_maps={"region": {}},
    ).fit(
        pd.DataFrame(
            {
                "region": ["north", "north", "south", None],
                "channel": ["web", "web", "store", "partner"],
                "value": [1, 2, 3, 4],
            }
        )
    )

    assert dump_mapping_artifact(empty_alias_transformer) == dump_mapping_artifact(
        transformer
    )
    assert json.loads(dump_mapping_artifact(transformer))["schema_version"] == 1


def test_alias_artifact_v2_is_deterministic_and_reproduces_transform() -> None:
    frame = pd.DataFrame(
        {"region": ["north", "nroth", "north", "south"], "value": range(4)}
    )
    first = CategoryConsolidationTransformer(
        columns=("region",),
        threshold_percent=60.0,
        alias_maps={"region": {"nroth": "north", "noth": "north"}},
    ).fit(frame)
    second = CategoryConsolidationTransformer(
        columns=("region",),
        threshold_percent=60.0,
        alias_maps={"region": {"noth": "north", "nroth": "north"}},
    ).fit(frame)
    payload = dump_mapping_artifact(first)
    restored = load_mapping_artifact(payload)
    inference = pd.DataFrame({"region": ["noth", "nroth", "North"], "value": range(3)})

    assert json.loads(payload)["schema_version"] == 2
    assert dump_mapping_artifact(second) == payload
    assert dump_mapping_artifact(restored) == payload
    assert restored.alias_maps_ == first.alias_maps_
    assert restored.transform(inference).equals(first.transform(inference))


def test_unfitted_transformer_cannot_be_serialized() -> None:
    transformer = CategoryConsolidationTransformer(
        columns=("region",), threshold_percent=10.0
    )

    with pytest.raises(NotFittedError):
        dump_mapping_artifact(transformer)


@pytest.mark.parametrize(
    "value",
    [None, False, True, -2, 3, -0.0, 1.25, "category", MISSING_CATEGORY],
)
def test_supported_scalar_codec_round_trip(value: object) -> None:
    decoded = decode_scalar(encode_scalar(value))

    if value is MISSING_CATEGORY:
        assert decoded is MISSING_CATEGORY
    else:
        assert decoded == value
        assert type(decoded) is type(value)


def test_nan_and_null_are_distinct_in_scalar_codec() -> None:
    encoded_nan = encode_scalar(float("nan"))
    encoded_null = encode_scalar(None)

    assert encoded_nan != encoded_null
    assert isnan(cast(float, decode_scalar(encoded_nan)))
    assert decode_scalar(encoded_null) is None
    assert decode_scalar(encode_scalar(float("inf"))) == float("inf")
    assert decode_scalar(encode_scalar(float("-inf"))) == float("-inf")


@pytest.mark.parametrize("value", [Decimal("1"), b"bytes", ("tuple",)])
def test_unsupported_scalar_types_fail_safely(value: object) -> None:
    with pytest.raises(TypeError, match="support only"):
        encode_scalar(value)


@pytest.mark.parametrize(
    "encoded",
    [
        None,
        {},
        {"type": "none", "value": None},
        {"type": "bool", "value": 1},
        {"type": "int", "value": "01"},
        {"type": "int", "value": "not-an-int"},
        {"type": "float", "value": "1.0e999"},
        {"type": "float", "value": "bad"},
        {"type": "unknown", "value": "x"},
        {"type": "str", "value": 1},
    ],
)
def test_invalid_encoded_scalars_are_rejected(encoded: object) -> None:
    with pytest.raises(ArtifactValidationError):
        decode_scalar(encoded)


def _mutated_payload(mutate: object) -> str:
    document = json.loads(dump_mapping_artifact(_fitted_transformer()))
    mutate(document)  # type: ignore[operator]
    body = {
        "schema_version": document["schema_version"],
        "transformer": document["transformer"],
    }
    import hashlib

    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    document["fingerprint"] = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    return json.dumps(document)


def test_unknown_schema_version_is_rejected() -> None:
    document = json.loads(dump_mapping_artifact(_fitted_transformer()))
    document["schema_version"] = 99

    with pytest.raises(ArtifactValidationError, match="schema_version"):
        load_mapping_artifact(json.dumps(document))


def test_malformed_schema_version_is_rejected() -> None:
    document = json.loads(dump_mapping_artifact(_fitted_transformer()))
    document["schema_version"] = []

    with pytest.raises(ArtifactValidationError, match="schema_version"):
        load_mapping_artifact(json.dumps(document))


def test_corrupt_fingerprint_is_rejected() -> None:
    document = json.loads(dump_mapping_artifact(_fitted_transformer()))
    document["transformer"]["threshold_percent"] = 99.0

    with pytest.raises(ArtifactValidationError, match="fingerprint"):
        load_mapping_artifact(json.dumps(document))


def test_fingerprint_must_be_a_string() -> None:
    document = json.loads(dump_mapping_artifact(_fitted_transformer()))
    document["fingerprint"] = 1

    with pytest.raises(ArtifactValidationError, match="must be a string"):
        load_mapping_artifact(json.dumps(document))


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"schema_version": 1, "schema_version": 1}',
        '{"value": NaN}',
    ],
)
def test_invalid_json_or_root_is_rejected(payload: str) -> None:
    with pytest.raises(ArtifactValidationError):
        load_mapping_artifact(payload)


def test_mappings_must_match_columns() -> None:
    payload = _mutated_payload(
        lambda document: document["transformer"]["mappings"].pop("channel")
    )

    with pytest.raises(ArtifactValidationError, match="mappings"):
        load_mapping_artifact(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda document: document.update(transformer=[]), "transformer"),
        (
            lambda document: document["transformer"].update(
                feature_names=["channel", "value"]
            ),
            "selected columns",
        ),
        (
            lambda document: document["transformer"].update(threshold_percent="bad"),
            "invalid transformer state",
        ),
        (
            lambda document: document["transformer"]["mappings"].update(region=[]),
            "column mapping",
        ),
        (
            lambda document: document["transformer"]["mappings"]["region"].update(
                observed_categories={}
            ),
            "must be a list",
        ),
        (
            lambda document: document["transformer"]["mappings"]["region"][
                "observed_categories"
            ].append(
                document["transformer"]["mappings"]["region"]["observed_categories"][0]
            ),
            "duplicates",
        ),
        (
            lambda document: document["transformer"].update(columns=["region", 1]),
            "list of strings",
        ),
        (
            lambda document: document["transformer"].update(columns=[]),
            "must not be empty",
        ),
        (
            lambda document: document["transformer"].update(
                columns=["region", "region"]
            ),
            "must be unique",
        ),
    ],
)
def test_invalid_artifact_state_is_rejected(mutate: object, message: str) -> None:
    with pytest.raises(ArtifactValidationError, match=message):
        load_mapping_artifact(_mutated_payload(mutate))


def test_retained_categories_must_be_observed() -> None:
    payload = _mutated_payload(
        lambda document: document["transformer"]["mappings"]["region"][
            "retained_categories"
        ].append({"type": "str", "value": "never-observed"})
    )

    with pytest.raises(ArtifactValidationError, match="must be observed"):
        load_mapping_artifact(payload)


def test_resolved_label_collision_is_rejected() -> None:
    def collide(document: dict[str, object]) -> None:
        region = document["transformer"]["mappings"]["region"]  # type: ignore[index]
        region["resolved_rare_label"] = region["observed_categories"][0]

    with pytest.raises(ArtifactValidationError, match="collides"):
        load_mapping_artifact(_mutated_payload(collide))


def _alias_payload() -> dict[str, object]:
    transformer = CategoryConsolidationTransformer(
        columns=("region",),
        threshold_percent=40.0,
        alias_maps={"region": {"nroth": "north"}},
    ).fit(_fitted_transformer_frame())
    return cast(dict[str, object], json.loads(dump_mapping_artifact(transformer)))


def _fitted_transformer_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "region": ["north", "north", "south", None],
            "channel": ["web", "web", "store", "partner"],
            "value": [1, 2, 3, 4],
        }
    )


def _resign(document: dict[str, object]) -> str:
    import hashlib

    body = {
        "schema_version": document["schema_version"],
        "transformer": document["transformer"],
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    document["fingerprint"] = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    return json.dumps(document)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda document: document["transformer"].update(alias_maps=[]), "object"),
        (
            lambda document: document["transformer"]["alias_maps"].update(region={}),
            "entries must be a list",
        ),
        (
            lambda document: document["transformer"]["alias_maps"].update(region=[[]]),
            "entry must be an object",
        ),
        (
            lambda document: document["transformer"]["alias_maps"]["region"][0].update(
                extra=True
            ),
            "keys must be exactly",
        ),
        (
            lambda document: document["transformer"]["alias_maps"]["region"].append(
                document["transformer"]["alias_maps"]["region"][0]
            ),
            "must not contain duplicates",
        ),
        (
            lambda document: document["transformer"].update(alias_maps={}),
            "requires at least one",
        ),
        (
            lambda document: document["transformer"]["alias_maps"]["region"][0].update(
                canonical={"type": "str", "value": "unknown"}
            ),
            "invalid transformer state",
        ),
    ],
)
def test_invalid_alias_artifact_state_is_rejected(
    mutate: object,
    message: str,
) -> None:
    document = _alias_payload()
    mutate(document)  # type: ignore[operator]

    with pytest.raises(ArtifactValidationError, match=message):
        load_mapping_artifact(_resign(document))
