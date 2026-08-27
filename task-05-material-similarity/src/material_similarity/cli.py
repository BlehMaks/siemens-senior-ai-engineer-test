"""Command-line access to description-based material alternatives."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import cast

from material_similarity.data import load_materials
from material_similarity.retrieval import rank_alternatives


def main(argv: Sequence[str] | None = None) -> int:
    """Print deterministic JSON for one part or the complete catalog."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "catalog", type=Path, help="Path to semicolon-delimited Fuse.csv"
    )
    parser.add_argument("--part-id", help="Return only this PART_ID")
    parser.add_argument("--output", type=Path, help="Write JSON to this path")
    args = parser.parse_args(argv)

    results = rank_alternatives(load_materials(cast(Path, args.catalog)))
    part_id = cast(str | None, args.part_id)
    if part_id is None:
        payload: object = [asdict(result) for result in results]
    else:
        result = next((result for result in results if result.part_id == part_id), None)
        if result is None:
            parser.error(f"unknown PART_ID: {part_id}")
        payload = asdict(result)

    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output = cast(Path | None, args.output)
    if output is None:
        print(rendered, end="")
    else:
        output.write_text(rendered, encoding="utf-8")
    return 0
