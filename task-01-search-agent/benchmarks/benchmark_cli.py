"""Validate, score, or dry-run the frozen M5 benchmark capture boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmark_protocol import (
    BenchmarkCapture,
    BenchmarkInputError,
    BenchmarkReport,
    fake_capture,
    load_capture,
    load_eval_manifest_hash,
    load_protocol,
    load_report,
    replay_report,
    score_capture,
    write_report_exclusive,
)

_BENCHMARK_DIR = Path(__file__).resolve().parent
_TASK_DIR = _BENCHMARK_DIR.parent


def _summary(report: BenchmarkReport, artifact: Path | None = None) -> str:
    payload: dict[str, object] = {
        "evidence_kind": report.evidence_kind,
        "selection": (
            report.selection.candidate_id if report.selection is not None else None
        ),
        "selection_reason": report.selection_reason,
        "eligible_candidates": [
            item.candidate_id for item in report.candidates if item.eligible
        ],
    }
    if artifact is not None:
        payload["artifact"] = str(artifact)
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _checked_report_hashes(
    report: BenchmarkReport, protocol_sha256: str, manifest_sha256: str
) -> None:
    if (
        report.protocol_sha256 != protocol_sha256
        or report.eval_manifest_sha256 != manifest_sha256
    ):
        raise BenchmarkInputError("stored report does not match frozen inputs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol", type=Path, default=_BENCHMARK_DIR / "protocol.json"
    )
    parser.add_argument(
        "--eval-manifest",
        type=Path,
        default=_TASK_DIR / "evals" / "cases" / "fixed.yaml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry_run = subparsers.add_parser("dry-run")
    dry_run.add_argument("--output-dir", type=Path, required=True)

    score = subparsers.add_parser("score")
    score.add_argument("--capture", type=Path, required=True)
    score.add_argument("--output-dir", type=Path, required=True)

    replay = subparsers.add_parser("replay")
    replay.add_argument(
        "--report",
        type=Path,
        default=_BENCHMARK_DIR / "fixtures" / "synthetic-report.json",
    )

    subparsers.add_parser("schema")
    args = parser.parse_args(argv)

    if args.command == "schema":
        print(
            json.dumps(
                {
                    "capture": BenchmarkCapture.model_json_schema(),
                    "result": BenchmarkReport.model_json_schema(),
                },
                sort_keys=True,
            )
        )
        return 0

    try:
        loaded = load_protocol(args.protocol)
        manifest_sha256 = load_eval_manifest_hash(args.eval_manifest, loaded.value)
        if args.command == "dry-run":
            capture = fake_capture(
                loaded.value,
                protocol_sha256=loaded.sha256,
                eval_manifest_sha256=manifest_sha256,
            )
            report = score_capture(
                capture,
                loaded.value,
                protocol_sha256=loaded.sha256,
                eval_manifest_sha256=manifest_sha256,
            )
            artifact = write_report_exclusive(report, args.output_dir)
        elif args.command == "score":
            capture = load_capture(args.capture, loaded.value)
            report = score_capture(
                capture,
                loaded.value,
                protocol_sha256=loaded.sha256,
                eval_manifest_sha256=manifest_sha256,
            )
            artifact = write_report_exclusive(report, args.output_dir)
        else:
            report = load_report(args.report, loaded.value.max_capture_bytes)
            _checked_report_hashes(report, loaded.sha256, manifest_sha256)
            report = replay_report(report)
            artifact = None
    except BenchmarkInputError:
        print('{"error":"invalid benchmark input","selection":null}')
        return 2

    print(_summary(report, artifact))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
