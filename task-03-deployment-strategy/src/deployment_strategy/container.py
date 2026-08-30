"""Bounded process entry point for the assessment service container."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI
from google.cloud.firestore_v1 import AsyncClient as FirestoreAsyncClient
from google.cloud.tasks_v2 import CloudTasksAsyncClient

import agent_api.storage.gcp as gcp_storage
from agent_api.app import create_app
from agent_api.observability import ReadinessProbe
from agent_api.security import EnvPepperProvider
from agent_api.storage import (
    CloudTasksWorkQueue,
    DocumentStore,
    FirestoreEventRepository,
    FirestoreRunRepository,
    FirestoreSessionRepository,
    GoogleCloudTaskClient,
    GoogleFirestoreDocumentStore,
    SignedWorkItemCodec,
)
from search_agent import OllamaResearchExecutor, OllamaRuntimeSettings
from search_agent.cli import _demo_runner
from search_agent.contracts import OpaqueId, QueryText
from search_agent.memory import (
    RepositoryReviewedMemoryReader,
    SQLiteProcedureRepository,
    SQLiteSemanticFactRepository,
)
from search_agent.runner import RunResult
from search_agent.runtime import search_backends_from_environment

from .model_auth import GoogleIdTokenAuth

_DEFAULT_DATABASE_PATH = Path("/tmp/agent-api.sqlite3")
_LOGGER = logging.getLogger(__name__)
_CLOUD_QUEUE = re.compile(
    r"^projects/(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])/"
    r"locations/[a-z]+-[a-z]+[0-9]/queues/[a-z][a-z0-9-]{1,99}$"
)


@dataclass(frozen=True, slots=True)
class CloudRuntimeSettings:
    project_id: str
    database: str
    queue_name: str
    delivery_path: str
    target_url: str | None
    service_role: str


@dataclass(frozen=True, slots=True)
class CloudAdapters:
    sessions: FirestoreSessionRepository
    runs: FirestoreRunRepository
    events: FirestoreEventRepository
    queue: CloudTasksWorkQueue
    readiness: ReadinessProbe


CloudAdapterFactory = Callable[[CloudRuntimeSettings, bytes], CloudAdapters]


class CloudReadinessProbe:
    """Check both managed dependencies without writing probe records."""

    def __init__(
        self, store: DocumentStore, task_client: GoogleCloudTaskClient
    ) -> None:
        self._store = store
        self._tasks = task_client

    async def ready(self) -> bool:
        try:
            await self._store.get(
                collection="_readiness", document_id="connectivity-probe"
            )
        except Exception as exc:
            _LOGGER.warning(
                "Cloud readiness failed: dependency=firestore error_type=%s",
                type(exc).__name__,
            )
            return False
        try:
            tasks_ready = await self._tasks.ready()
        except Exception as exc:
            _LOGGER.warning(
                "Cloud readiness failed: dependency=cloud_tasks error_type=%s",
                type(exc).__name__,
            )
            return False
        if not tasks_ready:
            _LOGGER.warning(
                "Cloud readiness failed: dependency=cloud_tasks status=unavailable"
            )
        return tasks_ready


class FakeRunExecutor:
    """Execute the deterministic offline agent used by container smoke tests."""

    async def run(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        request: QueryText,
    ) -> RunResult:
        return await _demo_runner(str(request)).run(
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
            request=str(request),
        )


def build_application(
    *, cloud_adapter_factory: CloudAdapterFactory | None = None
) -> FastAPI:
    """Build one API process from environment-only runtime configuration."""

    _configure_action_logging()
    database_path = Path(
        os.environ.get("AGENT_API_DATABASE_PATH", str(_DEFAULT_DATABASE_PATH))
    )
    if not database_path.is_absolute():
        raise ValueError("AGENT_API_DATABASE_PATH must be absolute")

    cloud_settings = _cloud_runtime_settings()
    inference_mode = os.environ.get("AGENT_API_INFERENCE_MODE", "fake")
    executor = _run_executor(
        inference_mode,
        cloud_settings=cloud_settings,
        database_path=database_path,
    )
    if cloud_settings is not None:
        if cloud_settings.service_role == "api" and executor is not None:
            raise ValueError("cloud API service must disable local inference")
        if cloud_settings.service_role == "worker" and executor is None:
            raise ValueError("cloud worker service requires an executor")
        signing_secret = EnvPepperProvider("AGENT_API_TASK_SIGNING_HMAC").pepper()
        factory = (
            _build_google_cloud_adapters
            if cloud_adapter_factory is None
            else cloud_adapter_factory
        )
        cloud = factory(cloud_settings, signing_secret)
    else:
        cloud = None

    return create_app(
        database_path=database_path,
        run_executor=executor,
        worker_shutdown_seconds=_bounded_integer(
            "AGENT_API_SHUTDOWN_SECONDS", default=10, minimum=1, maximum=30
        ),
        session_repository=None if cloud is None else cloud.sessions,
        run_repository=None if cloud is None else cloud.runs,
        event_repository=None if cloud is None else cloud.events,
        work_queue=None if cloud is None else cloud.queue,
        readiness_probe=None if cloud is None else cloud.readiness,
        production_environment=cloud is not None or bool(os.environ.get("K_SERVICE")),
        run_state_backend="sqlite" if cloud is None else "firestore",
        queue_backend="sqlite" if cloud is None else "cloud_tasks",
        queue_delivery_path=(
            os.environ.get(
                "AGENT_API_QUEUE_DELIVERY_PATH", "/internal/tasks/run-delivery"
            )
            if cloud_settings is None
            else cloud_settings.delivery_path
        ),
        task_delivery_enabled=(
            cloud_settings is not None and cloud_settings.service_role == "worker"
        ),
    )


def _run_executor(
    inference_mode: str,
    *,
    cloud_settings: CloudRuntimeSettings | None,
    database_path: Path,
) -> FakeRunExecutor | OllamaResearchExecutor | None:
    if inference_mode == "fake":
        return FakeRunExecutor()
    if inference_mode == "disabled":
        return None
    if inference_mode != "ollama":
        raise ValueError("AGENT_API_INFERENCE_MODE must be fake, ollama, or disabled")

    model_name = os.environ.get("AGENT_MODEL_NAME", "")
    default_profile = (
        "cloud"
        if cloud_settings is not None and cloud_settings.service_role == "worker"
        else "local"
    )
    transport_profile = os.environ.get("AGENT_MODEL_TRANSPORT_PROFILE", default_profile)
    if transport_profile not in {"local", "cloud"}:
        raise ValueError("AGENT_MODEL_TRANSPORT_PROFILE must be local or cloud")
    if (
        cloud_settings is not None
        and cloud_settings.service_role == "worker"
        and transport_profile != "cloud"
    ):
        raise ValueError("cloud Ollama worker requires cloud model transport")
    base_url = os.environ.get(
        "AGENT_MODEL_BASE_URL",
        "http://127.0.0.1:11434" if transport_profile == "local" else "",
    )
    audience = os.environ.get("AGENT_MODEL_GOOGLE_ID_TOKEN_AUDIENCE")
    settings = OllamaRuntimeSettings(
        model_name=model_name,
        base_url=base_url,
        transport_profile=cast(Literal["local", "cloud"], transport_profile),
        google_id_token_audience=audience,
        timeout_seconds=_bounded_integer(
            "AGENT_MODEL_TIMEOUT_SECONDS", default=120, minimum=1, maximum=600
        ),
        max_retries=_bounded_integer(
            "AGENT_MODEL_MAX_RETRIES", default=1, minimum=0, maximum=5
        ),
        search_region=os.environ.get("AGENT_SEARCH_REGION", "wt-wt"),
        search_backends=search_backends_from_environment(os.environ),
    )
    auth = (
        None
        if settings.google_id_token_audience is None
        else GoogleIdTokenAuth(settings.google_id_token_audience)
    )
    return OllamaResearchExecutor(
        settings=settings,
        model_auth=auth,
        memory_reader_factory=(
            None
            if cloud_settings is not None
            else lambda: _local_memory_reader(database_path)
        ),
    )


def _local_memory_reader(database_path: Path) -> RepositoryReviewedMemoryReader:
    """Open one request-scoped reader over the migrated local memory store."""

    return RepositoryReviewedMemoryReader(
        SQLiteSemanticFactRepository(database_path),
        SQLiteProcedureRepository(database_path),
    )


def _configure_action_logging() -> None:
    level_name = os.environ.get("AGENT_ACTION_LOG_LEVEL", "INFO")
    if level_name not in {"ERROR", "WARNING", "INFO", "DEBUG"}:
        raise ValueError(
            "AGENT_ACTION_LOG_LEVEL must be ERROR, WARNING, INFO, or DEBUG"
        )
    level = getattr(logging, level_name)
    for logger_name in (
        "search_agent.actions",
        "agent_api.operations",
        "deployment_strategy",
    ):
        logging.getLogger(logger_name).setLevel(level)


def _cloud_runtime_settings() -> CloudRuntimeSettings | None:
    names = (
        "AGENT_API_SERVICE_ROLE",
        "AGENT_API_GCP_PROJECT_ID",
        "AGENT_API_FIRESTORE_DATABASE",
        "AGENT_API_CLOUD_TASKS_QUEUE",
        "AGENT_API_TASK_TARGET_URL",
    )
    configured = {name: os.environ.get(name) for name in names}
    if not any(configured.values()):
        return None

    role = _required_env(configured, "AGENT_API_SERVICE_ROLE")
    if role not in {"api", "worker"}:
        raise ValueError("AGENT_API_SERVICE_ROLE must be api or worker")
    project_id = _required_env(configured, "AGENT_API_GCP_PROJECT_ID")
    database = _required_env(configured, "AGENT_API_FIRESTORE_DATABASE")
    queue_name = _required_env(configured, "AGENT_API_CLOUD_TASKS_QUEUE")
    delivery_path = os.environ.get(
        "AGENT_API_QUEUE_DELIVERY_PATH", "/internal/tasks/run-delivery"
    )
    if (
        not delivery_path.startswith("/")
        or urlsplit(delivery_path).path != delivery_path
    ):
        raise ValueError("AGENT_API_QUEUE_DELIVERY_PATH must be an absolute URL path")
    queue_match = _CLOUD_QUEUE.fullmatch(queue_name)
    if queue_match is None or queue_match.group("project") != project_id:
        raise ValueError(
            "AGENT_API_CLOUD_TASKS_QUEUE must belong to the configured project"
        )
    target_url = configured["AGENT_API_TASK_TARGET_URL"]
    if role == "api":
        target_url = _required_env(configured, "AGENT_API_TASK_TARGET_URL")
        parsed = urlsplit(target_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path != delivery_path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "AGENT_API_TASK_TARGET_URL must be a clean HTTPS URL for the delivery path"
            )
    elif target_url is not None:
        raise ValueError("AGENT_API_TASK_TARGET_URL belongs only on the API service")
    return CloudRuntimeSettings(
        project_id=project_id,
        database=database,
        queue_name=queue_name,
        delivery_path=delivery_path,
        target_url=target_url,
        service_role=role,
    )


def _required_env(values: dict[str, str | None], name: str) -> str:
    value = values[name]
    if value is None or not value or value.strip() != value:
        raise ValueError(f"{name} is required for cloud runtime")
    return value


def _build_google_cloud_adapters(
    settings: CloudRuntimeSettings, signing_secret: bytes
) -> CloudAdapters:
    store = GoogleFirestoreDocumentStore(
        cast(
            gcp_storage._FirestoreClient,
            FirestoreAsyncClient(
                project=settings.project_id,
                database=settings.database,
            ),
        )
    )
    runs = FirestoreRunRepository(store)
    task_client = GoogleCloudTaskClient(
        cast(gcp_storage._CloudTasksClient, CloudTasksAsyncClient()),
        queue_name=settings.queue_name,
        target_url=settings.target_url,
    )
    return CloudAdapters(
        sessions=FirestoreSessionRepository(store, runs),
        runs=runs,
        events=FirestoreEventRepository(store),
        queue=CloudTasksWorkQueue(
            store=store,
            task_client=task_client,
            queue_name=settings.queue_name,
            codec=SignedWorkItemCodec(signing_secret),
        ),
        readiness=CloudReadinessProbe(store, task_client),
    )


def main() -> None:
    """Run a single bounded Uvicorn worker and let it own SIGTERM handling."""

    uvicorn.run(
        "deployment_strategy.container:build_application",
        factory=True,
        host="0.0.0.0",
        port=_bounded_integer("PORT", default=8080, minimum=1, maximum=65_535),
        workers=1,
        access_log=False,
        date_header=False,
        proxy_headers=False,
        server_header=False,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=_bounded_integer(
            "AGENT_API_SHUTDOWN_SECONDS", default=10, minimum=1, maximum=30
        ),
    )


def _bounded_integer(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{name} must be a decimal integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the supported range")
    return value


if __name__ == "__main__":
    main()
