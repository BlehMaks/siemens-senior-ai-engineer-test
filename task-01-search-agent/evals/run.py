"""Run the fixed or provenance-recorded Task 1 evaluation suite."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness import EvalInputError, EvaluationReport, run_fixed, run_live

_EVAL_DIR = Path(__file__).resolve().parent


def _summary(report: EvaluationReport, artifact: Path | None = None) -> str:
    payload: dict[str, object] = {
        "mode": report.mode,
        "passed": report.passed,
        "case_count": report.case_count,
        "metrics": {
            name: metric.rate for name, metric in sorted(report.metrics.items())
        },
        "hard_gates": {
            name: gate.passed for name, gate in sorted(report.hard_gates.items())
        },
        "failed_cases": sorted(report.case_failures),
    }
    if artifact is not None:
        payload["artifact"] = str(artifact)
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixed", "live"), default="fixed")
    parser.add_argument(
        "--manifest", type=Path, default=_EVAL_DIR / "cases" / "fixed.yaml"
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=_EVAL_DIR / "fixtures" / "observations.json",
    )
    parser.add_argument("--source")
    parser.add_argument("--model")
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.mode == "fixed":
            report = run_fixed(args.manifest, args.fixtures)
            artifact = None
        else:
            if not args.source or not args.model or args.artifact_dir is None:
                parser.error("live mode requires --source, --model, and --artifact-dir")
            report, artifact = run_live(
                args.manifest,
                args.fixtures,
                source=args.source,
                model=args.model,
                artifact_dir=args.artifact_dir,
            )
    except EvalInputError:
        print('{"error":"invalid evaluation input","passed":false}')
        return 2

    print(_summary(report, artifact))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
