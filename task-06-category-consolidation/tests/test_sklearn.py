from __future__ import annotations

import math
import pickle
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from sklearn.base import clone  # type: ignore[import-untyped]
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
from sklearn.utils.validation import NotFittedError  # type: ignore[import-untyped]

from category_consolidation import MISSING_CATEGORY, UnhashableCategoryError
from category_consolidation.sklearn import CategoryConsolidationTransformer


def _training_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "region": ["north", "north", "south", None],
            "channel": ["web", "web", "store", "partner"],
            "value": [10, 20, 30, 40],
        },
        index=[10, 11, 12, 13],
    )


def test_multi_column_transform_preserves_dataframe_contract() -> None:
    frame = _training_frame()
    transformer = CategoryConsolidationTransformer(
        columns=["region", "channel"], threshold_percent=40.0
    )

    result = transformer.fit_transform(frame)

    assert isinstance(result, pd.DataFrame)
    assert result.index.equals(frame.index)
    assert result.columns.tolist() == frame.columns.tolist()
    assert result["value"].tolist() == frame["value"].tolist()
    assert result["region"].tolist() == [
        "north",
        "north",
        "__RARE__",
        "__RARE__",
    ]
    assert transformer.get_feature_names_out().tolist() == frame.columns.tolist()


def test_transform_uses_frozen_training_mapping_and_reports_drift() -> None:
    transformer = CategoryConsolidationTransformer(
        columns=("region",), threshold_percent=40.0
    ).fit(_training_frame())
    inference = pd.DataFrame(
        {
            "region": ["north", "west", None],
            "channel": ["web", "web", "web"],
            "value": [1, 2, 3],
        },
        index=[20, 21, 22],
    )

    result, diagnostics = transformer.transform_with_diagnostics(inference)

    assert result["region"].tolist() == ["north", "__RARE__", "__RARE__"]
    assert diagnostics["region"].row_count == 3
    assert diagnostics["region"].unseen_count == 1
    assert diagnostics["region"].unseen_rate == pytest.approx(1 / 3)
    assert diagnostics["region"].fallback_count == 2
    assert diagnostics["region"].fallback_rate == pytest.approx(2 / 3)
    assert diagnostics["region"].retained_category_count == 1


def test_empty_transform_has_zero_rates() -> None:
    transformer = CategoryConsolidationTransformer(
        columns=("region",), threshold_percent=40.0
    ).fit(_training_frame())

    _, diagnostics = transformer.transform_with_diagnostics(_training_frame().iloc[:0])

    assert diagnostics["region"].unseen_rate == 0.0
    assert diagnostics["region"].fallback_rate == 0.0


def test_nan_fallback_label_is_counted_in_diagnostics() -> None:
    transformer = CategoryConsolidationTransformer(
        columns=("region",),
        threshold_percent=100.0,
        rare_label=float("nan"),
    ).fit(pd.DataFrame({"region": ["north", "south"]}))

    result, diagnostics = transformer.transform_with_diagnostics(
        pd.DataFrame({"region": ["west"]})
    )

    assert math.isnan(result["region"].iloc[0])
    assert diagnostics["region"].fallback_count == 1
    assert diagnostics["region"].fallback_rate == 1.0


def test_clone_parameters_pipeline_and_pickle_are_supported() -> None:
    transformer = CategoryConsolidationTransformer(
        columns=("region",), threshold_percent=20.0, min_count=2
    )
    cloned = clone(transformer)
    cloned.set_params(threshold_percent=40.0)
    pipeline = Pipeline([("categories", cloned)]).fit(_training_frame())
    restored = pickle.loads(pickle.dumps(pipeline))

    assert transformer.get_params()["min_count"] == 2
    assert cloned.get_params()["threshold_percent"] == 40.0
    assert restored.transform(_training_frame()).equals(
        pipeline.transform(_training_frame())
    )


def test_explicit_aliases_are_applied_before_fit_and_frozen_for_inference() -> None:
    frame = pd.DataFrame(
        {
            "region": ["north", "north", "nroth", "south"],
            "value": range(4),
        }
    )
    policy = {"region": {"nroth": "north"}}
    configured = CategoryConsolidationTransformer(
        columns=("region",),
        threshold_percent=60.0,
        alias_maps=policy,
    )
    assert clone(configured).alias_maps == policy
    transformer = configured.fit(frame, y=["target", "values", "are", "ignored"])
    policy["region"]["nroth"] = "south"
    inference = pd.DataFrame({"region": ["nroth", "North", "north"], "value": range(3)})

    output, diagnostics = transformer.transform_with_diagnostics(inference)

    assert transformer.transform(frame)["region"].tolist() == [
        "north",
        "north",
        "north",
        "__RARE__",
    ]
    assert transformer.consolidators_["region"].retained_categories == frozenset(
        {"north"}
    )
    assert output["region"].tolist() == ["north", "__RARE__", "north"]
    assert diagnostics["region"].unseen_count == 1
    assert transformer.alias_maps_ == {"region": {"nroth": "north"}}


def test_default_and_empty_alias_policies_are_exact_no_ops() -> None:
    frame = _training_frame()
    default = CategoryConsolidationTransformer(
        columns=("region",), threshold_percent=40.0
    ).fit(frame)
    empty = CategoryConsolidationTransformer(
        columns=("region",),
        threshold_percent=40.0,
        alias_maps={"region": {}},
    ).fit(frame)

    assert empty.alias_maps_ == {}
    assert empty.transform(frame).equals(default.transform(frame))


@pytest.mark.parametrize(
    ("alias_maps", "exception", "message"),
    [
        ([], TypeError, "mapping by selected column"),
        ({1: {}}, TypeError, "column names must be strings"),
        ({"channel": {"store": "web"}}, ValueError, "not selected"),
        ({"region": []}, TypeError, "must be a mapping"),
        ({"region": {"nroth": ["north"]}}, TypeError, "must be hashable"),
        ({"region": {"north": "north"}}, ValueError, "cycle"),
        (
            {"region": {"nroth": "nrth", "nrth": "north"}},
            ValueError,
            "collisions",
        ),
        ({"region": {"nroth": "unknown"}}, ValueError, "unknown canonical"),
        ({"region": {"__RARE__": "north"}}, ValueError, "reserved"),
        ({"region": {"nroth": MISSING_CATEGORY}}, ValueError, "reserved"),
    ],
)
def test_invalid_alias_policies_are_rejected(
    alias_maps: object,
    exception: type[Exception],
    message: str,
) -> None:
    with pytest.raises(exception, match=message):
        CategoryConsolidationTransformer(
            columns=("region",),
            threshold_percent=40.0,
            alias_maps=alias_maps,  # type: ignore[arg-type]
        ).fit(_training_frame())


def test_multi_node_alias_cycle_is_rejected() -> None:
    with pytest.raises(ValueError, match="cycle"):
        CategoryConsolidationTransformer(
            columns=("region",),
            threshold_percent=40.0,
            alias_maps={"region": {"nroth": "nrth", "nrth": "nroth"}},
        ).fit(_training_frame())


def test_alias_validation_preserves_unhashable_value_error() -> None:
    frame = _training_frame()
    frame["region"] = pd.Series(
        [["north"], "north", "south", None], index=frame.index, dtype=object
    )

    with pytest.raises(UnhashableCategoryError, match="index 0"):
        CategoryConsolidationTransformer(
            columns=("region",),
            threshold_percent=40.0,
            alias_maps={"region": {"nroth": "north"}},
        ).fit(frame)


def test_set_output_pandas_preserves_dataframe() -> None:
    transformer = CategoryConsolidationTransformer(
        columns=("region",), threshold_percent=40.0
    ).set_output(transform="pandas")

    result = transformer.fit_transform(_training_frame())

    assert isinstance(result, pd.DataFrame)


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (pd.DataFrame([[1, 2]], columns=["a", "a"]), "unique"),
        (pd.DataFrame([[1]], columns=[1]), "strings"),
    ],
)
def test_invalid_dataframe_schema_is_rejected(
    frame: pd.DataFrame, message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        CategoryConsolidationTransformer(columns=("a",), threshold_percent=10.0).fit(
            frame
        )


@pytest.mark.parametrize(
    ("columns", "exception", "message"),
    [
        ("region", TypeError, "sequence"),
        ((), ValueError, "at least one"),
        (("region", 1), TypeError, "strings"),
        (("region", "region"), ValueError, "unique"),
        (("unknown",), ValueError, "missing"),
    ],
)
def test_invalid_selected_columns_are_rejected(
    columns: object, exception: type[Exception], message: str
) -> None:
    with pytest.raises(exception, match=message):
        CategoryConsolidationTransformer(
            columns=columns,  # type: ignore[arg-type]
            threshold_percent=10.0,
        ).fit(_training_frame())


def test_transform_rejects_non_dataframe_before_fit_and_schema_changes() -> None:
    transformer = CategoryConsolidationTransformer(
        columns=("region",), threshold_percent=10.0
    )
    with pytest.raises(NotFittedError):
        transformer.transform(_training_frame())
    with pytest.raises(TypeError, match="DataFrame"):
        transformer.fit([["north"]])  # type: ignore[arg-type]

    transformer.fit(_training_frame())
    with pytest.raises(ValueError, match=r"missing=.*channel"):
        transformer.transform(_training_frame().drop(columns=["channel"]))
    with pytest.raises(ValueError, match=r"extra=.*new"):
        transformer.transform(_training_frame().assign(new=1))
    with pytest.raises(ValueError, match="schema and order"):
        transformer.transform(_training_frame()[["channel", "region", "value"]])
    duplicate = _training_frame()
    duplicate.columns = ["region", "channel", "channel"]
    with pytest.raises(ValueError, match="unique"):
        transformer.transform(duplicate)


def test_feature_name_validation() -> None:
    transformer = CategoryConsolidationTransformer(
        columns=("region",), threshold_percent=10.0
    )
    with pytest.raises(NotFittedError):
        transformer.get_feature_names_out()

    transformer.fit(_training_frame())
    with pytest.raises(ValueError, match="input_features"):
        transformer.get_feature_names_out(["wrong"])


def test_package_core_import_does_not_require_optional_dependencies() -> None:
    package_src = Path(__file__).parents[1] / "src"
    script = f"""
import importlib.abc
import sys

class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {{'pandas', 'sklearn'}}:
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, BlockOptional())
sys.path.insert(0, {str(package_src)!r})
import category_consolidation
assert category_consolidation.consolidate_rare_categories(['a'], 0) == ['a']
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
