from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import httpx
import pytest

from agent_api.ports import WorkItem
from agent_api.storage import (
    CloudTask,
    CloudTasksWorkQueue,
    DocumentStoreTransaction,
    FirestoreEventRepository,
    FirestoreRunRepository,
    FirestoreSessionRepository,
    SignedWorkItemCodec,
)
from deployment_strategy.container import (
    CloudAdapters,
    CloudReadinessProbe,
    CloudRuntimeSettings,
    FakeRunExecutor,
    _bounded_integer,
    _run_executor,
    build_application,
    main,
)
from deployment_strategy.model_auth import GoogleIdTokenAuth
from search_agent import OllamaResearchExecutor
from search_agent.state import RunStatus

TASK_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TASK_ROOT.parent
DOCKERFILE = TASK_ROOT / "container" / "Dockerfile"
T = TypeVar("T")


def test_main_builds_cloud_clients_inside_uvicorn_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(app: object, **options: object) -> None:
        captured["app"] = app
        captured.update(options)

    monkeypatch.setattr("deployment_strategy.container.uvicorn.run", run)

    main()

    assert captured["app"] == "deployment_strategy.container:build_application"
    assert captured["factory"] is True


class CloudFakeStore(DocumentStoreTransaction):
    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None:
        del collection, document_id
        return None

    async def set(
        self, *, collection: str, document_id: str, document: Mapping[str, object]
    ) -> None:
        del collection, document_id, document

    async def delete(self, *, collection: str, document_id: str) -> bool:
        del collection, document_id
        return False

    async def list(
        self,
        *,
        collection: str,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        start_after: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        del collection, filters, order_by, start_after, limit
        return ()

    async def transaction(
        self, operation: Callable[[DocumentStoreTransaction], Awaitable[T]]
    ) -> T:
        return await operation(self)


class CloudFakeTaskClient:
    async def create(self, task: CloudTask) -> CloudTask:
        return task

    async def get(self, *, name: str) -> CloudTask | None:
        del name
        return None

    async def delete(self, *, name: str) -> bool:
        del name
        return False


class ReadyProbe:
    async def ready(self) -> bool:
        return True


class NotReadyProbe:
    async def ready(self) -> bool:
        return False


class ExplodingStore(CloudFakeStore):
    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None:
        del collection, document_id
        raise RuntimeError("test-only failure")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cloud_readiness_logs_the_failed_dependency(
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe = CloudReadinessProbe(ExplodingStore(), ReadyProbe())

    assert await probe.ready() is False
    assert (
        "Cloud readiness failed: dependency=firestore error_type=RuntimeError"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_cloud_readiness_logs_an_unavailable_task_queue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe = CloudReadinessProbe(CloudFakeStore(), NotReadyProbe())

    assert await probe.ready() is False
    assert (
        "Cloud readiness failed: dependency=cloud_tasks status=unavailable"
        in caplog.text
    )


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
    assert "target=/tmp/uv-cache" in dockerfile
    for security_revision in (
        "libcrypto3=3.5.8-r0",
        "libssl3=3.5.8-r0",
        "sqlite-libs=3.53.4-r0",
    ):
        assert security_revision in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "COPY --from=builder /opt/venv /opt/venv" in dockerfile
    assert "--chown" not in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert dockerfile.index("WORKDIR /app") < dockerfile.index("USER 65532:65532")
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert 'CMD ["python", "-I", "-B", "-c"' in dockerfile
    assert (
        'ENTRYPOINT ["python", "-I", "-u", "-B", "-m", '
        '"deployment_strategy.container"]' in dockerfile
    )
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


@pytest.mark.parametrize("mode", ["cloud", "FAKE", " fake"])
def test_unknown_inference_modes_fail_closed(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_API_DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("AGENT_API_INFERENCE_MODE", mode)

    with pytest.raises(ValueError, match="AGENT_API_INFERENCE_MODE"):
        build_application()


def test_local_ollama_mode_reuses_the_bounded_research_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_MODEL_NAME", "granite3.3:8b")

    executor = _run_executor(
        "ollama", cloud_settings=None, database_path=tmp_path / "memory.sqlite3"
    )

    assert isinstance(executor, OllamaResearchExecutor)
    assert executor.settings.model_name == "granite3.3:8b"
    assert executor.settings.base_url == "http://127.0.0.1:11434"
    assert executor.settings.transport_profile == "local"
    assert executor.settings.search_backends == ("auto",)
    assert executor.model_auth is None
    assert executor.memory_reader_factory is not None


def test_ollama_mode_rejects_a_missing_model_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_MODEL_NAME", raising=False)

    with pytest.raises(ValueError, match="model_name"):
        _run_executor(
            "ollama", cloud_settings=None, database_path=tmp_path / "memory.sqlite3"
        )


def test_cloud_worker_ollama_requires_matching_private_model_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cloud_environment(monkeypatch, tmp_path, role="worker")
    monkeypatch.setenv("AGENT_API_INFERENCE_MODE", "ollama")
    monkeypatch.setenv("AGENT_MODEL_NAME", "granite3.3:8b")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://private-model.example.run.app")
    monkeypatch.setenv("AGENT_MODEL_TRANSPORT_PROFILE", "cloud")

    with pytest.raises(ValueError, match="audience"):
        build_application(cloud_adapter_factory=_cloud_factory({}))

    monkeypatch.setenv(
        "AGENT_MODEL_GOOGLE_ID_TOKEN_AUDIENCE",
        "https://different-model.example.run.app",
    )
    with pytest.raises(ValueError, match="exactly match"):
        build_application(cloud_adapter_factory=_cloud_factory({}))


def test_cloud_worker_ollama_uses_google_identity_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cloud_environment(monkeypatch, tmp_path, role="worker")
    model_origin = "https://private-model.example.run.app"
    monkeypatch.setenv("AGENT_API_INFERENCE_MODE", "ollama")
    monkeypatch.setenv("AGENT_MODEL_NAME", "granite3.3:8b")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", model_origin)
    monkeypatch.setenv("AGENT_MODEL_TRANSPORT_PROFILE", "cloud")
    monkeypatch.setenv("AGENT_MODEL_GOOGLE_ID_TOKEN_AUDIENCE", model_origin)

    executor = _run_executor(
        "ollama",
        cloud_settings=CloudRuntimeSettings(
            project_id="contract-assessment-dev",
            database="(default)",
            queue_name=(
                "projects/contract-assessment-dev/locations/europe-west3/"
                "queues/dispatch"
            ),
            delivery_path="/internal/tasks/run-delivery",
            target_url=None,
            service_role="worker",
        ),
        database_path=tmp_path / "memory.sqlite3",
    )

    assert isinstance(executor, OllamaResearchExecutor)
    assert isinstance(executor.model_auth, GoogleIdTokenAuth)
    assert executor.settings.transport_profile == "cloud"
    assert executor.settings.google_id_token_audience == model_origin
    assert executor.memory_reader_factory is None


def test_local_process_can_use_authenticated_cloud_ollama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_origin = "https://private-model.example.run.app"
    monkeypatch.setenv("AGENT_MODEL_NAME", "granite3.3:8b")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", model_origin)
    monkeypatch.setenv("AGENT_MODEL_TRANSPORT_PROFILE", "cloud")
    monkeypatch.setenv("AGENT_MODEL_GOOGLE_ID_TOKEN_AUDIENCE", model_origin)
    monkeypatch.setenv("AGENT_SEARCH_BACKENDS", "auto,duckduckgo")

    executor = _run_executor(
        "ollama", cloud_settings=None, database_path=tmp_path / "memory.sqlite3"
    )

    assert isinstance(executor, OllamaResearchExecutor)
    assert isinstance(executor.model_auth, GoogleIdTokenAuth)
    assert executor.settings.search_backends == ("auto", "duckduckgo")
    assert executor.memory_reader_factory is not None


def test_cloud_worker_rejects_local_model_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_MODEL_NAME", "granite3.3:8b")
    monkeypatch.setenv("AGENT_MODEL_TRANSPORT_PROFILE", "local")

    with pytest.raises(ValueError, match="requires cloud model transport"):
        _run_executor(
            "ollama",
            cloud_settings=CloudRuntimeSettings(
                project_id="contract-assessment-dev",
                database="(default)",
                queue_name=(
                    "projects/contract-assessment-dev/locations/europe-west3/"
                    "queues/dispatch"
                ),
                delivery_path="/internal/tasks/run-delivery",
                target_url=None,
                service_role="worker",
            ),
            database_path=tmp_path / "memory.sqlite3",
        )


def test_relative_database_paths_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_API_DATABASE_PATH", "state/agent-api.sqlite3")

    with pytest.raises(ValueError, match="AGENT_API_DATABASE_PATH"):
        build_application()


def test_action_log_level_is_strict_and_configures_agent_loggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_API_DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("AGENT_API_KEY_PEPPER", "c" * 43)
    monkeypatch.setenv("AGENT_ACTION_LOG_LEVEL", "debug")
    with pytest.raises(ValueError, match="AGENT_ACTION_LOG_LEVEL"):
        build_application()

    monkeypatch.setenv("AGENT_ACTION_LOG_LEVEL", "DEBUG")
    build_application()

    assert logging.getLogger("search_agent.actions").level == logging.DEBUG
    assert logging.getLogger("agent_api.operations").level == logging.DEBUG


def test_cloud_run_production_rejects_sqlite_authoritative_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_API_DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("AGENT_API_INFERENCE_MODE", "disabled")
    monkeypatch.setenv("K_SERVICE", "agent-api")

    with pytest.raises(ValueError, match="Firestore run state"):
        build_application()


def test_cloud_run_production_rejects_partial_cloud_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_API_DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("AGENT_API_INFERENCE_MODE", "disabled")
    monkeypatch.setenv("K_SERVICE", "agent-api")
    monkeypatch.setenv("AGENT_API_FIRESTORE_DATABASE", "(default)")

    with pytest.raises(ValueError, match="AGENT_API_SERVICE_ROLE"):
        build_application()


def _cloud_factory(
    captured: dict[str, object],
) -> Callable[[CloudRuntimeSettings, bytes], CloudAdapters]:
    def build(settings: CloudRuntimeSettings, secret: bytes) -> CloudAdapters:
        store = CloudFakeStore()
        runs = FirestoreRunRepository(store)
        codec = SignedWorkItemCodec(secret)
        queue = CloudTasksWorkQueue(
            store=store,
            task_client=CloudFakeTaskClient(),
            queue_name=settings.queue_name,
            codec=codec,
        )
        captured.update(settings=settings, codec=codec)
        return CloudAdapters(
            sessions=FirestoreSessionRepository(store, runs),
            runs=runs,
            events=FirestoreEventRepository(store),
            queue=queue,
            readiness=ReadyProbe(),
        )

    return build


def _set_cloud_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    role: str,
) -> None:
    secret = base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
    pepper = base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    monkeypatch.setenv("AGENT_API_DATABASE_PATH", str(tmp_path / f"{role}.sqlite3"))
    monkeypatch.setenv("AGENT_API_KEY_PEPPER", pepper)
    monkeypatch.setenv("AGENT_API_TASK_SIGNING_HMAC", secret)
    monkeypatch.setenv("AGENT_API_SERVICE_ROLE", role)
    monkeypatch.setenv("AGENT_API_GCP_PROJECT_ID", "contract-assessment-dev")
    monkeypatch.setenv("AGENT_API_FIRESTORE_DATABASE", "(default)")
    monkeypatch.setenv(
        "AGENT_API_CLOUD_TASKS_QUEUE",
        "projects/contract-assessment-dev/locations/europe-west3/queues/dispatch",
    )
    monkeypatch.setenv(
        "AGENT_API_INFERENCE_MODE", "disabled" if role == "api" else "fake"
    )
    monkeypatch.setenv("K_SERVICE", f"agent-{role}")
    if role == "api":
        monkeypatch.setenv(
            "AGENT_API_TASK_TARGET_URL",
            "https://worker.example.run.app/internal/tasks/run-delivery",
        )


@pytest.mark.asyncio
async def test_cloud_fake_api_selects_managed_adapters_and_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cloud_environment(monkeypatch, tmp_path, role="api")
    captured: dict[str, object] = {}
    app = build_application(cloud_adapter_factory=_cloud_factory(captured))

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://container"
        ) as client,
    ):
        ready = await client.get("/health/ready")
        assert app.state.internal_worker is None
        assert isinstance(app.state.work_queue, CloudTasksWorkQueue)

    settings = captured["settings"]
    assert isinstance(settings, CloudRuntimeSettings)
    assert settings.service_role == "api"
    assert settings.delivery_path == "/internal/tasks/run-delivery"
    assert settings.target_url == (
        "https://worker.example.run.app/internal/tasks/run-delivery"
    )
    assert ready.status_code == 200


@pytest.mark.asyncio
async def test_cloud_fake_worker_acknowledges_signed_orphan_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cloud_environment(monkeypatch, tmp_path, role="worker")
    captured: dict[str, object] = {}
    app = build_application(cloud_adapter_factory=_cloud_factory(captured))
    settings = captured["settings"]
    codec = captured["codec"]
    assert isinstance(settings, CloudRuntimeSettings)
    assert isinstance(codec, SignedWorkItemCodec)
    item = WorkItem(
        work_id="work-cloud-smoke",
        tenant_id="tenant-cloud",
        run_id="run-cloud",
        generation_id="generation-cloud-smoke",
        enqueued_at=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
        not_before=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
    )
    task_identity = f"{item.work_id}\x00{item.generation_id}"
    task_id = hashlib.sha256(task_identity.encode()).hexdigest()[:32]
    task_name = f"{settings.queue_name}/tasks/work-{task_id}"
    body, signed_headers = codec.encode(
        item,
        task_name=task_name,
        queue_name=settings.queue_name,
    )
    headers = dict(signed_headers) | {
        "X-CloudTasks-TaskName": task_name.rsplit("/", 1)[-1],
        "X-CloudTasks-QueueName": settings.queue_name.rsplit("/", 1)[-1],
    }

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://container"
        ) as client,
    ):
        accepted = await client.post(
            "/internal/tasks/run-delivery", content=body, headers=headers
        )
        rejected = await client.post(
            "/internal/tasks/run-delivery", content=body + b" ", headers=headers
        )
        assert app.state.internal_worker is not None

    assert accepted.status_code == 204
    assert rejected.status_code == 401


@pytest.mark.parametrize(
    "missing",
    [
        "AGENT_API_GCP_PROJECT_ID",
        "AGENT_API_FIRESTORE_DATABASE",
        "AGENT_API_CLOUD_TASKS_QUEUE",
        "AGENT_API_TASK_TARGET_URL",
        "AGENT_API_TASK_SIGNING_HMAC",
    ],
)
def test_cloud_configuration_fails_closed_when_required_values_are_missing(
    missing: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cloud_environment(monkeypatch, tmp_path, role="api")
    monkeypatch.delenv(missing)

    with pytest.raises((ValueError, RuntimeError)):
        build_application(cloud_adapter_factory=_cloud_factory({}))


def test_cloud_target_must_match_delivery_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cloud_environment(monkeypatch, tmp_path, role="api")
    monkeypatch.setenv("AGENT_API_QUEUE_DELIVERY_PATH", "/internal/tasks/custom")

    with pytest.raises(ValueError, match="delivery path"):
        build_application(cloud_adapter_factory=_cloud_factory({}))
