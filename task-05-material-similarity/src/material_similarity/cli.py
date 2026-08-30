"""Command-line access to description-based material alternatives."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import cast

from material_similarity.data import load_materials
from material_similarity.hybrid import (
    BusinessRetrievalResult,
    HybridRetrievalResult,
    load_compatibility_policy,
    rank_business_alternatives,
    rank_hybrid_alternatives,
)
from material_similarity.retrieval import (
    RetrievalResult,
    rank_alternatives,
    rank_complete_alternatives,
)


def main(argv: Sequence[str] | None = None) -> int:
    """Print deterministic JSON for one part or the complete catalog."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "catalog", type=Path, help="Path to semicolon-delimited Fuse.csv"
    )
    parser.add_argument(
        "--mode",
        choices=("complete", "text", "hybrid", "extension"),
        default="complete",
        help=(
            "Return complete labeled results, the reviewed text baseline, or the "
            "non-promoted hybrid prototype, or the opt-in version-2 business mode"
        ),
    )
    parser.add_argument(
        "--policy",
        type=Path,
        help="Reviewed JSON-compatible YAML policy for hybrid or extension mode",
    )
    parser.add_argument("--part-id", help="Return only this PART_ID")
    parser.add_argument("--output", type=Path, help="Write JSON to this path")
    args = parser.parse_args(argv)

    materials = load_materials(cast(Path, args.catalog))
    results: tuple[
        RetrievalResult | HybridRetrievalResult | BusinessRetrievalResult, ...
    ]
    policy_path = cast(Path | None, args.policy)
    policy = load_compatibility_policy(policy_path) if policy_path is not None else None
    if args.mode == "extension":
        results = rank_business_alternatives(materials, policy=policy)
    elif args.mode == "hybrid":
        results = rank_hybrid_alternatives(materials, policy=policy)
    elif args.mode == "text":
        if policy is not None:
            parser.error("--policy requires hybrid or extension mode")
        results = rank_alternatives(materials)
    else:
        if policy is not None:
            parser.error("--policy requires hybrid or extension mode")
        results = rank_complete_alternatives(materials)
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
