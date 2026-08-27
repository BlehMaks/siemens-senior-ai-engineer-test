from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

TASK_ROOT = Path(__file__).resolve().parents[1]
GCP_OPS = TASK_ROOT / "scripts" / "gcp_ops.sh"
API_SMOKE = TASK_ROOT / "scripts" / "api_smoke.sh"


def _executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _fake_tools(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gcloud_log = tmp_path / "gcloud.log"
    terraform_log = tmp_path / "terraform.log"
    _executable(
        fake_bin / "gcloud",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_GCLOUD_LOG"
case "$1 $2" in
  "projects describe") printf '%s\n' "${FAKE_PROJECT_NUMBER:-123456789012}" ;;
  "auth list") printf '%s\n' "operator@example.com" ;;
  "services list") printf '%s\n' run.googleapis.com cloudtasks.googleapis.com firestore.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com ;;
  "billing budgets") printf '%s\n' "${FAKE_BUDGET-sai-dev-budget}" ;;
  "run revisions") printf '%s\n' "${FAKE_REVISION_JSON}" ;;
  "run services")
    if [[ ${3:-} == describe && " $* " == *" --format=json "* ]]; then
      printf '%s\n' "${FAKE_SERVICE_JSON}"
    elif [[ ${3:-} == describe ]]; then
      if [[ -n ${FAKE_RESOURCE_ERROR:-} ]]; then
        printf '%s\n' "${FAKE_RESOURCE_ERROR}" >&2
        exit 7
      elif [[ ${FAKE_RESOURCE_EXISTS:-0} == 1 ]]; then
        exit 0
      fi
      printf 'NOT_FOUND: service is absent\n' >&2
      exit 1
    fi
    ;;
  "tasks queues")
    if [[ ${3:-} == describe ]]; then
      if [[ -n ${FAKE_RESOURCE_ERROR:-} ]]; then
        printf '%s\n' "${FAKE_RESOURCE_ERROR}" >&2
        exit 7
      elif [[ ${FAKE_RESOURCE_EXISTS:-0} == 1 ]]; then
        exit 0
      fi
      printf 'NOT_FOUND: queue is absent\n' >&2
      exit 1
    fi
    ;;
  *) printf 'unexpected gcloud command: %s\n' "$*" >&2; exit 9 ;;
esac
""",
    )
    _executable(
        fake_bin / "terraform",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_TERRAFORM_LOG"
for argument in "$@"; do
  if [[ $argument == -out=* ]]; then
    : > "${argument#-out=}"
  fi
done
if [[ " $* " == *" show -json "* ]]; then
  if [[ ${FAKE_PLAN_RESOURCE_TYPE:-} == google_service_account_iam_member ]]; then
    printf '{"variables":{"project_id":{"value":"%s"},"project_number":{"value":"%s"},"region":{"value":"%s"},"system_code":{"value":"%s"}},"resource_changes":[{"address":"module.run_services.google_service_account_iam_member.tasks_service_agent_token_creator","type":"google_service_account_iam_member","change":{"actions":["delete"],"before":{"service_account_id":"%s"}}}]}\n' \
      "${FAKE_PLAN_PROJECT_ID:-contract-assessment-dev}" \
      "${FAKE_PLAN_PROJECT_NUMBER:-123456789012}" \
      "${FAKE_PLAN_REGION:-europe-west3}" \
      "${FAKE_PLAN_SYSTEM_CODE:-sai}" \
      "${FAKE_PLAN_SERVICE_ACCOUNT_ID:-projects/contract-assessment-dev/serviceAccounts/sai-dev-tasks@contract-assessment-dev.iam.gserviceaccount.com}"
  else
    printf '{"variables":{"project_id":{"value":"%s"},"project_number":{"value":"%s"},"region":{"value":"%s"},"system_code":{"value":"%s"}},"resource_changes":[{"address":"module.run_services.google_cloud_run_v2_service.api","type":"google_cloud_run_v2_service","change":{"actions":["delete"],"before":{"project":"%s","location":"%s","labels":{"environment":"dev","system":"sai"}}}}]}\n' \
      "${FAKE_PLAN_PROJECT_ID:-contract-assessment-dev}" \
      "${FAKE_PLAN_PROJECT_NUMBER:-123456789012}" \
      "${FAKE_PLAN_REGION:-europe-west3}" \
      "${FAKE_PLAN_SYSTEM_CODE:-sai}" \
      "${FAKE_PLAN_RESOURCE_PROJECT:-contract-assessment-dev}" \
      "${FAKE_PLAN_RESOURCE_REGION:-europe-west3}"
  fi
elif [[ " $* " == *" show "* ]]; then
  printf 'reviewed destroy plan\n'
fi
""",
    )
    return fake_bin, gcloud_log, terraform_log


def _ops_environment(
    fake_bin: Path, gcloud_log: Path, terraform_log: Path
) -> dict[str, str]:
    ready_revision = {
        "metadata": {
            "name": "sai-dev-api-00002-abc",
            "labels": {"serving.knative.dev/service": "sai-dev-api"},
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    service = {
        "metadata": {"name": "sai-dev-api"},
        "status": {
            "traffic": [{"revisionName": "sai-dev-api-00002-abc", "percent": 100}]
        },
    }
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_GCLOUD_LOG": str(gcloud_log),
        "FAKE_TERRAFORM_LOG": str(terraform_log),
        "FAKE_REVISION_JSON": json.dumps(ready_revision),
        "FAKE_SERVICE_JSON": json.dumps(service),
    }


def _run_ops(
    environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(GCP_OPS), *arguments],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def test_operations_scripts_have_valid_bash_syntax() -> None:
    subprocess.run(["bash", "-n", str(GCP_OPS), str(API_SMOKE)], check=True)


def test_preflight_is_read_only_and_requires_a_budget(tmp_path: Path) -> None:
    fake_bin, gcloud_log, terraform_log = _fake_tools(tmp_path)
    environment = _ops_environment(fake_bin, gcloud_log, terraform_log)

    passed = _run_ops(
        environment,
        "preflight",
        "contract-assessment-dev",
        "europe-west3",
        "dev",
        "123456789012",
        "ABCDEF-123456-ABCDEF",
    )
    assert passed.returncode == 0, passed.stderr
    assert "preflight passed" in passed.stdout
    assert "update-traffic" not in gcloud_log.read_text(encoding="utf-8")

    environment["FAKE_BUDGET"] = ""
    failed = _run_ops(
        environment,
        "preflight",
        "contract-assessment-dev",
        "europe-west3",
        "dev",
        "123456789012",
        "ABCDEF-123456-ABCDEF",
    )
    assert failed.returncode != 0
    assert "budget" in failed.stderr


def test_rollback_uses_only_a_ready_revision_owned_by_the_service(
    tmp_path: Path,
) -> None:
    fake_bin, gcloud_log, terraform_log = _fake_tools(tmp_path)
    environment = _ops_environment(fake_bin, gcloud_log, terraform_log)
    arguments = (
        "rollback",
        "contract-assessment-dev",
        "europe-west3",
        "dev",
        "123456789012",
        "sai-dev-api",
        "sai-dev-api-00002-abc",
    )

    passed = _run_ops(environment, *arguments)
    assert passed.returncode == 0, passed.stderr
    assert "update-traffic sai-dev-api" in gcloud_log.read_text(encoding="utf-8")
    assert "sai-dev-api-00002-abc=100" in gcloud_log.read_text(encoding="utf-8")

    gcloud_log.write_text("", encoding="utf-8")
    unready = json.loads(environment["FAKE_REVISION_JSON"])
    unready["status"]["conditions"][0]["status"] = "False"
    environment["FAKE_REVISION_JSON"] = json.dumps(unready)
    failed = _run_ops(environment, *arguments)
    assert failed.returncode != 0
    assert "not a ready revision" in failed.stderr
    assert "update-traffic" not in gcloud_log.read_text(encoding="utf-8")


def test_project_mismatch_blocks_rollback_before_traffic_mutation(
    tmp_path: Path,
) -> None:
    fake_bin, gcloud_log, terraform_log = _fake_tools(tmp_path)
    environment = _ops_environment(fake_bin, gcloud_log, terraform_log)

    failed = _run_ops(
        environment,
        "rollback",
        "contract-assessment-dev",
        "europe-west3",
        "dev",
        "999999999999",
        "sai-dev-api",
        "sai-dev-api-00002-abc",
    )
    assert failed.returncode != 0
    assert "project number does not match" in failed.stderr
    assert "update-traffic" not in gcloud_log.read_text(encoding="utf-8")


def test_teardown_requires_exact_confirmation_and_verifies_runtime_removal(
    tmp_path: Path,
) -> None:
    fake_bin, gcloud_log, terraform_log = _fake_tools(tmp_path)
    environment = _ops_environment(fake_bin, gcloud_log, terraform_log)
    terraform_root = tmp_path / "environment"
    terraform_root.mkdir()
    (terraform_root / "main.tf").write_text("terraform {}\n", encoding="utf-8")
    common = (
        "teardown",
        "contract-assessment-dev",
        "europe-west3",
        "dev",
        "123456789012",
        "sai",
        str(terraform_root),
    )

    refused = _run_ops(environment, *common, "DESTROY:wrong:dev")
    assert refused.returncode != 0
    assert not terraform_log.exists()

    passed = _run_ops(environment, *common, "DESTROY:contract-assessment-dev:dev")
    assert passed.returncode == 0, passed.stderr
    terraform_calls = terraform_log.read_text(encoding="utf-8")
    assert "plan -destroy" in terraform_calls
    assert " show " in f" {terraform_calls} "
    assert " apply " in f" {terraform_calls} "
    assert "runtime teardown verified" in passed.stdout


def test_teardown_fails_closed_when_absence_cannot_be_verified(
    tmp_path: Path,
) -> None:
    fake_bin, gcloud_log, terraform_log = _fake_tools(tmp_path)
    environment = _ops_environment(fake_bin, gcloud_log, terraform_log)
    environment["FAKE_RESOURCE_ERROR"] = "PERMISSION_DENIED: inventory unavailable"
    terraform_root = tmp_path / "environment"
    terraform_root.mkdir()
    (terraform_root / "main.tf").write_text("terraform {}\n", encoding="utf-8")

    result = _run_ops(
        environment,
        "teardown",
        "contract-assessment-dev",
        "europe-west3",
        "dev",
        "123456789012",
        "sai",
        str(terraform_root),
        "DESTROY:contract-assessment-dev:dev",
    )

    assert result.returncode != 0
    assert "could not verify absence" in result.stderr
    assert "runtime teardown verified" not in result.stdout


def test_teardown_binds_destroy_plan_to_confirmed_target(tmp_path: Path) -> None:
    fake_bin, gcloud_log, terraform_log = _fake_tools(tmp_path)
    environment = _ops_environment(fake_bin, gcloud_log, terraform_log)
    environment["FAKE_PLAN_PROJECT_ID"] = "unrelated-production-project"
    terraform_root = tmp_path / "environment"
    terraform_root.mkdir()
    (terraform_root / "main.tf").write_text("terraform {}\n", encoding="utf-8")

    result = _run_ops(
        environment,
        "teardown",
        "contract-assessment-dev",
        "europe-west3",
        "dev",
        "123456789012",
        "sai",
        str(terraform_root),
        "DESTROY:contract-assessment-dev:dev",
    )

    assert result.returncode != 0
    assert "destroy plan does not match" in result.stderr
    assert " apply " not in f" {terraform_log.read_text(encoding='utf-8')} "


def test_teardown_rejects_delete_from_another_project_in_state(
    tmp_path: Path,
) -> None:
    fake_bin, gcloud_log, terraform_log = _fake_tools(tmp_path)
    environment = _ops_environment(fake_bin, gcloud_log, terraform_log)
    environment["FAKE_PLAN_RESOURCE_PROJECT"] = "unrelated-production-project"
    terraform_root = tmp_path / "environment"
    terraform_root.mkdir()
    (terraform_root / "main.tf").write_text("terraform {}\n", encoding="utf-8")

    result = _run_ops(
        environment,
        "teardown",
        "contract-assessment-dev",
        "europe-west3",
        "dev",
        "123456789012",
        "sai",
        str(terraform_root),
        "DESTROY:contract-assessment-dev:dev",
    )

    assert result.returncode != 0
    assert "destroy plan does not match" in result.stderr
    assert " apply " not in f" {terraform_log.read_text(encoding='utf-8')} "


def test_teardown_accepts_project_bound_service_account_iam_delete(
    tmp_path: Path,
) -> None:
    fake_bin, gcloud_log, terraform_log = _fake_tools(tmp_path)
    environment = _ops_environment(fake_bin, gcloud_log, terraform_log)
    environment["FAKE_PLAN_RESOURCE_TYPE"] = "google_service_account_iam_member"
    terraform_root = tmp_path / "environment"
    terraform_root.mkdir()
    (terraform_root / "main.tf").write_text("terraform {}\n", encoding="utf-8")

    result = _run_ops(
        environment,
        "teardown",
        "contract-assessment-dev",
        "europe-west3",
        "dev",
        "123456789012",
        "sai",
        str(terraform_root),
        "DESTROY:contract-assessment-dev:dev",
    )

    assert result.returncode == 0, result.stderr
    assert " apply " in f" {terraform_log.read_text(encoding='utf-8')} "


@pytest.mark.parametrize("script", [GCP_OPS, API_SMOKE])
def test_cleanup_traps_treat_hostile_tmpdir_as_data(
    tmp_path: Path, script: Path
) -> None:
    marker = tmp_path / "trap-injected"
    hostile_tmp = tmp_path / "hostile'; touch trap-injected; #"
    hostile_tmp.mkdir()
    fake_bin, gcloud_log, terraform_log = _fake_tools(tmp_path)
    environment = _ops_environment(fake_bin, gcloud_log, terraform_log)
    environment["TMPDIR"] = str(hostile_tmp)

    if script == GCP_OPS:
        terraform_root = tmp_path / "environment"
        terraform_root.mkdir()
        (terraform_root / "main.tf").write_text("terraform {}\n", encoding="utf-8")
        _run_ops(
            environment,
            "teardown",
            "contract-assessment-dev",
            "europe-west3",
            "dev",
            "123456789012",
            "sai",
            str(terraform_root),
            "DESTROY:contract-assessment-dev:dev",
        )
    else:
        _executable(fake_bin / "curl", "#!/usr/bin/env bash\nexit 7\n")
        subprocess.run(
            [str(API_SMOKE), "http://127.0.0.1:1", "review-001"],
            check=False,
            capture_output=True,
            cwd=tmp_path,
            env={
                **environment,
                "SMOKE_API_KEY_A": "key-a",
                "SMOKE_API_KEY_B": "key-b",
            },
            text=True,
        )

    assert not marker.exists()


class SmokeHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str, str | None]]] = []
    run_count: ClassVar[int] = 0
    leak_cross_tenant: ClassVar[bool] = False

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _record(self) -> None:
        self.requests.append(
            (self.command, self.path, self.headers.get("Authorization"))
        )
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)

    def _json(self, status: int, document: dict[str, object]) -> None:
        body = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._record()
        authorization = self.headers.get("Authorization")
        if self.path in {"/health/live", "/health/ready"}:
            self._json(200, {"status": "ok"})
        elif self.path == "/v1/sessions" and authorization is None:
            self._json(401, {"error": {"code": "unauthorized"}})
        elif self.path == "/v1/sessions/session-smoke":
            status = (
                200
                if authorization == "Bearer key-b" and self.leak_cross_tenant
                else 404
            )
            self._json(status, {"session_id": "session-smoke"})
        elif self.path == "/v1/runs/run-stream/events":
            body = b'id: 1\nevent: run.created\ndata: {"sequence":1}\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(404, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:
        self._record()
        if self.path == "/v1/sessions":
            self._json(201, {"session_id": "session-smoke"})
        elif self.path == "/v1/sessions/session-smoke/runs":
            type(self).run_count += 1
            run_id = "run-stream" if self.run_count == 1 else "run-cancel"
            self._json(202, {"run_id": run_id})
        elif self.path == "/v1/runs/run-cancel/cancel":
            self._json(202, {"cancellation_requested": True})
        else:
            self._json(404, {"error": {"code": "not_found"}})

    def do_DELETE(self) -> None:
        self._record()
        if self.path == "/v1/sessions/session-smoke":
            self.send_response(204)
            self.end_headers()
        else:
            self._json(404, {"error": {"code": "not_found"}})


def _run_api_smoke(*, leak_cross_tenant: bool) -> subprocess.CompletedProcess[str]:
    SmokeHandler.requests = []
    SmokeHandler.run_count = 0
    SmokeHandler.leak_cross_tenant = leak_cross_tenant
    server = ThreadingHTTPServer(("127.0.0.1", 0), SmokeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return subprocess.run(
            [str(API_SMOKE), f"http://127.0.0.1:{server.server_port}", "review-001"],
            check=False,
            capture_output=True,
            env={
                **os.environ,
                "SMOKE_API_KEY_A": "key-a",
                "SMOKE_API_KEY_B": "key-b",
            },
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_api_smoke_covers_health_auth_run_sse_cancel_tenant_and_delete() -> None:
    result = _run_api_smoke(leak_cross_tenant=False)
    assert result.returncode == 0, result.stderr
    assert "API smoke passed" in result.stdout
    assert (
        "POST",
        "/v1/runs/run-cancel/cancel",
        "Bearer key-a",
    ) in SmokeHandler.requests
    assert (
        SmokeHandler.requests.count(
            ("GET", "/v1/sessions/session-smoke", "Bearer key-a")
        )
        == 1
    )


def test_api_smoke_fails_on_cross_tenant_visibility() -> None:
    result = _run_api_smoke(leak_cross_tenant=True)
    assert result.returncode != 0
    assert "cross-tenant isolation returned HTTP 200" in result.stderr


def test_every_smoke_request_has_a_wall_clock_timeout() -> None:
    source = API_SMOKE.read_text(encoding="utf-8")
    request_helper = source.split("request_status() {", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]

    assert "--max-time" in request_helper
    assert "--max-time 30" in source


class ErrorSseHandler(SmokeHandler):
    def do_GET(self) -> None:
        if self.path != "/v1/runs/run-stream/events":
            super().do_GET()
            return
        self._record()
        body = b'event: run.failed\ndata: {"sequence":1}\n\n'
        self.send_response(500)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_api_smoke_rejects_error_sse_with_event_shaped_body() -> None:
    ErrorSseHandler.requests = []
    ErrorSseHandler.run_count = 0
    ErrorSseHandler.leak_cross_tenant = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), ErrorSseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                str(API_SMOKE),
                f"http://127.0.0.1:{server.server_port}",
                "review-001",
            ],
            check=False,
            capture_output=True,
            env={
                **os.environ,
                "SMOKE_API_KEY_A": "key-a",
                "SMOKE_API_KEY_B": "key-b",
            },
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert result.returncode != 0
    assert "SSE stream returned HTTP 500" in result.stderr
    assert "API smoke passed" not in result.stdout
