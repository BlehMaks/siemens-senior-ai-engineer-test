from __future__ import annotations

import importlib.util
from pathlib import Path

_SUPPORT_SPEC = importlib.util.spec_from_file_location(
    "cloud_api_smoke_script",
    Path(__file__).with_name("test_cloud_api_smoke_script.py"),
)
assert _SUPPORT_SPEC is not None
assert _SUPPORT_SPEC.loader is not None
_SUPPORT = importlib.util.module_from_spec(_SUPPORT_SPEC)
_SUPPORT_SPEC.loader.exec_module(_SUPPORT)
_fixture = _SUPPORT._fixture
_run = _SUPPORT._run
_write_executable = _SUPPORT._write_executable


def _rewrite_gcloud(wrapper: Path, *, replacement: str) -> None:
    gcloud = wrapper.parents[3] / "bin" / "gcloud"
    _write_executable(
        gcloud,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'gcloud %s\\n' "$*" >> "$FAKE_COMMAND_LOG"
if [[ "$1 $2" == "projects describe" ]]; then
  {replacement}
  printf '1027058459333\\n'
  exit 0
fi
printf 'unexpected gcloud command: %s\\n' "$*" >&2
exit 9
""",
    )


def test_cloud_smoke_rejects_directory_substitution_before_uv(tmp_path: Path) -> None:
    wrapper, environment = _fixture(tmp_path)
    api_smoke = wrapper.parent / "api_smoke.sh"
    _rewrite_gcloud(
        wrapper,
        replacement=f'rm "{api_smoke}"; mkdir "{api_smoke}"',
    )

    result = _run(wrapper, environment)

    assert result.returncode != 0
    assert "API smoke script" in result.stderr
    assert "uv " not in Path(environment["FAKE_COMMAND_LOG"]).read_text(
        encoding="utf-8"
    )


def test_cloud_smoke_rejects_in_place_rewrite_before_uv(tmp_path: Path) -> None:
    wrapper, environment = _fixture(tmp_path)
    api_smoke = wrapper.parent / "api_smoke.sh"
    _rewrite_gcloud(
        wrapper,
        replacement=f"printf 'rewritten\\n' > \"{api_smoke}\"",
    )

    result = _run(wrapper, environment)

    assert result.returncode != 0
    assert "contents changed" in result.stderr
    assert "uv " not in Path(environment["FAKE_COMMAND_LOG"]).read_text(
        encoding="utf-8"
    )


def test_cloud_smoke_rejects_rewrite_during_private_copy(tmp_path: Path) -> None:
    wrapper, environment = _fixture(tmp_path)
    api_smoke = wrapper.parent / "api_smoke.sh"
    fake_bin = Path(environment["PATH"].split(":", 1)[0])
    _write_executable(
        fake_bin / "cat",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'rewritten during copy\n' > "{api_smoke}"
exec /bin/cat "$@"
""",
    )

    result = _run(wrapper, environment)

    assert result.returncode != 0
    assert "changed while creating the private copy" in result.stderr
    log = Path(environment["FAKE_COMMAND_LOG"])
    assert not log.exists() or "uv " not in log.read_text(encoding="utf-8")


def test_cloud_smoke_runs_the_pinned_copy_on_normal_success(tmp_path: Path) -> None:
    wrapper, environment = _fixture(tmp_path)

    result = _run(wrapper, environment)

    assert result.returncode == 0, result.stderr
    log = Path(environment["FAKE_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "api-smoke https://sai-dev-api-example.run.app review-001" in log
