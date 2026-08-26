from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from binary_classification.evaluate import _error_slices


@pytest.mark.parametrize(
    ("literal", "other", "other_bucket"),
    [
        ("__MISSING__", None, "bucket:missing"),
        ("__OTHER__", "unseen", "bucket:other"),
    ],
)
def test_error_slice_buckets_do_not_collide_with_literal_categories(
    literal: str, other: object, other_bucket: str
) -> None:
    training = pd.DataFrame(
        {
            "category": [literal] * 6 + ["ordinary"] * 5,
            "id": range(11),
            "Class": ["n", "y"] * 5 + ["n"],
        }
    )
    holdout = pd.DataFrame(
        {
            "category": [literal, other],
            "id": [20, 21],
            "Class": ["n", "y"],
        }
    )

    slices = _error_slices(
        training,
        holdout,
        holdout["Class"].eq("n"),
        np.array([0.9, 0.1]),
        0.5,
        ("category",),
    )
    category_slices = [item for item in slices if item.dimension == "category"]

    assert {item.value for item in category_slices} == {
        f"value:{literal}",
        other_bucket,
    }
    assert [item.rows for item in category_slices] == [1, 1]
