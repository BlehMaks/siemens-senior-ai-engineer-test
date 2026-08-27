from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

from material_similarity.data import load_materials
from material_similarity.retrieval import TOP_K, rank_alternatives

_DEFAULT_CATALOG = Path(__file__).parents[2] / "input" / "IT DA AI Tasks" / "Fuse.csv"
_CATALOG = Path(os.environ.get("SIEMENS_FUSE_CSV", _DEFAULT_CATALOG))


@pytest.mark.skipif(not _CATALOG.is_file(), reason="employer Fuse.csv is not public")
def test_full_catalog_has_one_honest_result_per_part_id() -> None:
    materials = load_materials(_CATALOG)

    results = rank_alternatives(materials)

    assert len(results) == len(materials) == 998
    assert len({result.part_id for result in results}) == 998
    assert Counter(result.status for result in results) == {
        "ok": 663,
        "insufficient_description": 335,
    }
    assert all(
        len(result.alternatives) == TOP_K for result in results if result.status == "ok"
    )
    assert all(
        alternative.shared_tokens or alternative.shared_character_ngrams
        for result in results
        for alternative in result.alternatives
    )
