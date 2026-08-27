from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from benchmark_cli import main


def test_dry_run_is_offline_deterministic_and_exclusive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def network_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry-run must not access the network")

    monkeypatch.setattr(socket, "create_connection", network_forbidden)

    assert main(["dry-run", "--output-dir", str(tmp_path)]) == 0
    first = json.loads(capsys.readouterr().out)
    artifact = Path(first["artifact"])
    original = artifact.read_bytes()
    assert first["evidence_kind"] == "synthetic"
    assert first["selection"] is None

    assert main(["dry-run", "--output-dir", str(tmp_path)]) == 2
    second = json.loads(capsys.readouterr().out)
    assert second == {"error": "invalid benchmark input", "selection": None}
    assert artifact.read_bytes() == original


def test_stored_synthetic_report_replays_without_selection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["replay"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["evidence_kind"] == "synthetic"
    assert output["selection"] is None
    assert output["eligible_candidates"] == []


def test_schema_command_exposes_strict_capture_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["schema"]) == 0

    schemas = json.loads(capsys.readouterr().out)
    assert schemas["capture"]["additionalProperties"] is False
    assert schemas["result"]["additionalProperties"] is False
    assert "protocol_sha256" in schemas["capture"]["properties"]
    assert "selection" in schemas["result"]["properties"]


def test_score_rejects_malformed_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capture = tmp_path / "capture.json"
    capture.write_text("{}", encoding="utf-8")

    assert (
        main(
            [
                "score",
                "--capture",
                str(capture),
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["selection"] is None
