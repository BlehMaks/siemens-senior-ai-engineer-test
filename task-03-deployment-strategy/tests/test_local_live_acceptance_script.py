from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TASK_ROOT = Path(__file__).resolve().parents[1]
LIVE_ACCEPTANCE = TASK_ROOT / "scripts" / "local_live_acceptance.sh"
LIVE_SMOKE = TASK_ROOT / "scripts" / "live_api_smoke.sh"


class _LiveApiHandler(BaseHTTPRequestHandler):
    grounded = True
    source_url = "https://www.siemens.com/global/en/company/sustainability.html"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health/ready":
            self._send(200, {"status": "ready"})
            return
        if self.path == "/v1/runs/run-live":
            citations: list[dict[str, str]] = []
            if self.grounded:
                citations.append(
                    {
                        "claim": "Siemens states a reviewed sustainability commitment.",
                        "evidence_id": "evidence-live-1",
                        "source_url": self.source_url,
                    }
                )
            self._send(
                200,
                {
                    "state": "completed",
                    "answer": {
                        "answer_text": "A sufficiently long live research answer for validation.",
                        "citations": citations,
                    },
                },
            )
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        if self.path == "/v1/sessions":
            self._send(201, {"session_id": "session-live"})
            return
        if self.path == "/v1/sessions/session-live/runs":
            self._send(202, {"run_id": "run-live"})
            return
        self._send(404, {"error": "not_found"})


def _run_live_smoke(
    tmp_path: Path,
    *,
    grounded: bool,
    source_url: str = "https://www.siemens.com/global/en/company/sustainability.html",
) -> subprocess.CompletedProcess[str]:
    _LiveApiHandler.grounded = grounded
    _LiveApiHandler.source_url = source_url
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LiveApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "LIVE_API_KEY": os.urandom(24).hex(),
        "LIVE_MODEL_NAME": "qwen3:8b",
        "LIVE_RESEARCH_QUERY": "Research current Siemens sustainability commitments.",
        "LIVE_RUN_TIMEOUT_SECONDS": "60",
        "PYTHON_BIN": sys.executable,
    }
    try:
        return subprocess.run(
            [
                str(LIVE_SMOKE),
                f"http://127.0.0.1:{server.server_port}",
                str(tmp_path / "live-output"),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_live_acceptance_is_explicitly_ollama_backed_and_user_friendly() -> None:
    source = LIVE_ACCEPTANCE.read_text(encoding="utf-8")

    assert LIVE_ACCEPTANCE.stat().st_mode & 0o111
    assert "Install/start Ollama and download a model if needed" in source
    assert "already installed; configure and test only" in source
    assert "--setup install|existing" in source
    assert "ollama serve" in source
    assert 'ollama pull "$model_name"' in source
    assert "AGENT_API_INFERENCE_MODE=ollama" in source
    assert "AGENT_MODEL_TRANSPORT_PROFILE=local" in source
    assert "AGENT_SEARCH_BACKENDS=brave,auto" in source
    assert "AGENT_SEARCH_BACKENDS=duckduckgo" not in source
    assert "AGENT_API_INFERENCE_MODE=fake" not in source
    assert "live_api_smoke.sh" in source
    assert "api/ps" in source


def test_live_smoke_requires_a_grounded_completed_answer() -> None:
    source = LIVE_SMOKE.read_text(encoding="utf-8")

    assert LIVE_SMOKE.stat().st_mode & 0o111
    assert '.state == "completed"' in source
    assert ".answer.citations" in source
    assert ".source_url" in source
    assert "LIVE_RUN_TIMEOUT_SECONDS" in source
    assert "Ollama-backed run completed with public-web citations" in source


def test_live_scripts_parse_and_help_without_external_services() -> None:
    for script in (LIVE_ACCEPTANCE, LIVE_SMOKE):
        subprocess.run(["bash", "-n", str(script)], check=True)

    result = subprocess.run(
        [str(LIVE_ACCEPTANCE), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--setup install|existing" in result.stdout
    assert "--keep-running" in result.stdout


def test_live_smoke_accepts_a_completed_grounded_response(tmp_path: Path) -> None:
    result = _run_live_smoke(tmp_path, grounded=True)

    assert result.returncode == 0, result.stderr
    assert "Live API smoke passed" in result.stdout
    summary = json.loads((tmp_path / "live-output" / "summary.json").read_text())
    assert summary["mode"] == "ollama-live"
    assert summary["model"] == "qwen3:8b"
    assert summary["citation_count"] == 1


def test_live_smoke_uses_a_scope_stable_default_query() -> None:
    source = LIVE_SMOKE.read_text(encoding="utf-8")

    assert "Find the latest official Siemens sustainability report." in source


def test_live_smoke_rejects_an_ungrounded_response(tmp_path: Path) -> None:
    result = _run_live_smoke(tmp_path, grounded=False)

    assert result.returncode != 0
    assert "grounded answer with citations" in result.stderr


def test_live_smoke_rejects_private_loopback_citations(tmp_path: Path) -> None:
    result = _run_live_smoke(
        tmp_path,
        grounded=True,
        source_url="http://127.0.0.1/private-evidence",
    )

    assert result.returncode != 0
    assert "public-web" in result.stderr


@pytest.mark.parametrize(
    "source_url",
    [
        "http://127.0.0.01/private-evidence",
        "https://www.siemens.com:80/private-evidence",
        "https://reviewer.test/private-evidence",
    ],
    ids=["alternate-loopback", "wrong-scheme-port", "reserved-test-domain"],
)
def test_live_smoke_rejects_public_web_validation_bypasses(
    tmp_path: Path,
    source_url: str,
) -> None:
    result = _run_live_smoke(
        tmp_path,
        grounded=True,
        source_url=source_url,
    )

    assert result.returncode != 0, result.stdout
    assert "public-web" in result.stderr


@pytest.mark.parametrize(
    "source_url",
    [
        "https://exam\nple.com/private-evidence",
        "https://exam\rple.com/private-evidence",
        "https://exam\tple.com/private-evidence",
    ],
    ids=["line-feed", "carriage-return", "tab"],
)
def test_live_smoke_rejects_control_characters_in_citation_hosts(
    tmp_path: Path,
    source_url: str,
) -> None:
    result = _run_live_smoke(
        tmp_path,
        grounded=True,
        source_url=source_url,
    )

    assert result.returncode != 0, result.stdout
    assert "public-web" in result.stderr


def test_local_live_api_entrypoint_binds_only_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deployment_strategy.container import main as container_main

    captured: dict[str, object] = {}

    def run(app: object, **options: object) -> None:
        captured["app"] = app
        captured.update(options)

    monkeypatch.setattr("deployment_strategy.container.uvicorn.run", run)

    container_main()

    assert captured["host"] == "127.0.0.1"
