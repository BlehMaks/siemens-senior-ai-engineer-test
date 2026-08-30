from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

TASK_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = TASK_ROOT / "scripts" / "migrate_firestore_index_state.sh"
LEGACY_NAMES = (
    "sessions",
    "runs",
    "run_events",
    "audit_entries",
    "quota_execution_leases_active",
    "quota_sse_leases_active",
)


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _run_migration(
    tmp_path: Path, scenario: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    terraform_root = tmp_path / "terraform"
    terraform_root.mkdir()
    (terraform_root / "main.tf").write_text("terraform {}\n", encoding="utf-8")
    command_log = tmp_path / "commands.log"
    fake_terraform = tmp_path / "terraform-fake"
    _write_executable(
        fake_terraform,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_COMMAND_LOG"
shift
case "$1 $2" in
  "state list")
    if [[ $FAKE_SCENARIO == fresh ]]; then
      printf 'No state file was found!\n' >&2
      exit 1
    fi
    prefix='module.managed_services.google_firestore_index.'
    for name in sessions runs run_events audit_entries quota_execution_leases_active quota_sse_leases_active; do
      if [[ $FAKE_SCENARIO == replacement_named ]]; then
        printf '%sassessment_%s\n' "$prefix" "$name"
      elif [[ $FAKE_SCENARIO == both_named ]]; then
        printf '%s%s\n%sassessment_%s\n' "$prefix" "$name" "$prefix" "$name"
      else
        printf '%s%s\n' "$prefix" "$name"
      fi
    done
    ;;
  "state show")
    address=$4
    project=liquidity-planning-platform
    database=sai-dev
    if [[ $FAKE_SCENARIO == legacy_default ]]; then database='(default)'; fi
    if [[ $FAKE_SCENARIO == mixed_invalid ]]; then
      database='(default)'
      if [[ $address == *.runs ]]; then project=another-project; fi
    fi
    if [[ $FAKE_SCENARIO == wrong_database ]]; then database=unexpected-db; fi
    if [[ $FAKE_SCENARIO == wrong_project ]]; then project=another-project; fi
    printf 'project = "%s"\ndatabase = "%s"\n' "$project" "$database"
    ;;
  "state rm"|"state mv") ;;
  *) printf 'unexpected Terraform command: %s\n' "$*" >&2; exit 9 ;;
esac
""",
    )
    environment = {
        **os.environ,
        "FAKE_COMMAND_LOG": str(command_log),
        "FAKE_SCENARIO": scenario,
        "TERRAFORM_BIN": str(fake_terraform),
    }
    result = subprocess.run(
        [
            str(MIGRATION),
            str(terraform_root),
            "liquidity-planning-platform",
            "sai-dev",
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    log = command_log.read_text(encoding="utf-8")
    return result, log


@pytest.mark.parametrize("scenario", ["fresh", "legacy_named"])
def test_migration_is_a_no_op_for_safe_state(tmp_path: Path, scenario: str) -> None:
    result, log = _run_migration(tmp_path, scenario)

    assert result.returncode == 0, result.stderr
    assert " state rm " not in f" {log} "
    assert " state mv " not in f" {log} "


def test_migration_forgets_only_default_database_indexes(tmp_path: Path) -> None:
    result, log = _run_migration(tmp_path, "legacy_default")

    assert result.returncode == 0, result.stderr
    assert log.count(" state rm -lock-timeout=60s ") == len(LEGACY_NAMES)
    assert " state mv " not in f" {log} "
    for name in LEGACY_NAMES:
        assert f"module.managed_services.google_firestore_index.{name}" in log


def test_migration_restores_intermediate_addresses_without_recreating_indexes(
    tmp_path: Path,
) -> None:
    result, log = _run_migration(tmp_path, "replacement_named")

    assert result.returncode == 0, result.stderr
    assert log.count(" state mv -lock-timeout=60s ") == len(LEGACY_NAMES)
    assert " state rm " not in f" {log} "
    for name in LEGACY_NAMES:
        assert (
            "module.managed_services.google_firestore_index."
            f"assessment_{name} module.managed_services.google_firestore_index.{name}"
            in log
        )


@pytest.mark.parametrize(
    "scenario", ["wrong_project", "wrong_database", "mixed_invalid", "both_named"]
)
def test_migration_fails_closed_for_ambiguous_ownership(
    tmp_path: Path,
    scenario: str,
) -> None:
    result, log = _run_migration(tmp_path, scenario)

    assert result.returncode != 0
    assert " state rm " not in f" {log} "
    assert " state mv " not in f" {log} "
