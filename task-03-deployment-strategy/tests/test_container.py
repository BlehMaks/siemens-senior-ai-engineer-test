from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from deployment_strategy.container import (
    FakeRunExecutor,
    _bounded_integer,
    build_application,
)
from search_agent.state import RunStatus

TASK_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TASK_ROOT.parent
DOCKERFILE = TASK_ROOT / "container" / "Dockerfile"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dockerfile_is_locked_minimal_and_runtime_hardened() -> None:
    dockerfile = read(DOCKERFILE)

    assert (
        dockerfile.count(
            "python:3.12.14-alpine3.23@sha256:"
            "31a768b01976652c222e318fe5bd6e7c252f056cbf489c88fa256f1bf0af58e3"
        )
        == 1
    )
    assert (
        "ghcr.io/astral-sh/uv:0.12.6@sha256:"
        "88bc6eb1ccd4b82efd0e1b530caffabddf50dc2bf612e66c14ea25b8ee8a4d3d" in dockerfile
    )
    assert "docker/dockerfile:1.7@sha256:" in dockerfile
    assert "pip install" not in dockerfile
    for security_revision in (
        "libcrypto3=3.5.8-r0",
        "libssl3=3.5.8-r0",
        "sqlite-libs=3.53.4-r0",
    ):
        assert security_revision in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "COPY --from=builder --chown=65532:65532 /opt/venv /opt/venv" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "curl" not in dockerfile
    assert "wget" not in dockerfile
    assert "AGENT_API_KEY_PEPPER" not in dockerfile


def test_docker_context_excludes_secrets_caches_tests_and_private_input() -> None:
    ignored = read(REPOSITORY_ROOT / ".dockerignore")

    for excluded in (
        ".git",
        ".local",
        "input",
        "**/.env*",
        "**/.terraform",
        "**/*secret*",
        "task-01-search-agent/tests",
        "task-02-agent-api/tests",
    ):
        assert excluded in ignored.splitlines()


@pytest.mark.asyncio
async def test_fake_executor_completes_without_network_or_model() -> None:
    result = await FakeRunExecutor().run(
        tenant_id="tenant-one",
        session_id="session-one",
        run_id="run-one",
        request="Find the Siemens sustainability report.",
    )

    assert result.snapshot.status is RunStatus.COMPLETED
    assert result.snapshot.answer is not None
    assert str(result.snapshot.answer.citations[0].source_url).startswith(
        "https://www.siemens.com/"
    )


@pytest.mark.asyncio
async def test_application_is_ready_with_read_only_compatible_tmp_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "agent-api.sqlite3"
    pepper = base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    monkeypatch.setenv("AGENT_API_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("AGENT_API_KEY_PEPPER", pepper)
    monkeypatch.setenv("AGENT_API_INFERENCE_MODE", "fake")
    app = build_application()

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://container"
        ) as client,
    ):
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

    assert live.status_code == ready.status_code == 200
    assert ready.json()["status"] == "ok"
    assert database_path.exists()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PORT", "0"),
        ("PORT", "65536"),
        ("PORT", "+8080"),
        ("PORT", "\uff18\uff10\uff18\uff10"),
        ("AGENT_API_SHUTDOWN_SECONDS", "31"),
    ],
)
def test_bounded_process_settings_fail_closed(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    minimum, maximum = (1, 65_535) if name == "PORT" else (1, 30)

    with pytest.raises(ValueError, match=name):
        _bounded_integer(name, default=10, minimum=minimum, maximum=maximum)


@pytest.mark.parametrize("mode", ["ollama", "cloud", "FAKE", " fake"])
def test_unknown_inference_modes_fail_closed(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_API_DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("AGENT_API_INFERENCE_MODE", mode)

    with pytest.raises(ValueError, match="AGENT_API_INFERENCE_MODE"):
        build_application()


def test_relative_database_paths_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_API_DATABASE_PATH", "state/agent-api.sqlite3")

    with pytest.raises(ValueError, match="AGENT_API_DATABASE_PATH"):
        build_application()
