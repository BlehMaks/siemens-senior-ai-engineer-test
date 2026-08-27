"""Bounded process entry point for the assessment service container."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from agent_api.app import create_app
from search_agent.cli import _demo_runner
from search_agent.contracts import OpaqueId, QueryText
from search_agent.runner import RunResult

_DEFAULT_DATABASE_PATH = Path("/tmp/agent-api.sqlite3")


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


def build_application() -> FastAPI:
    """Build one API process from environment-only runtime configuration."""

    database_path = Path(
        os.environ.get("AGENT_API_DATABASE_PATH", str(_DEFAULT_DATABASE_PATH))
    )
    if not database_path.is_absolute():
        raise ValueError("AGENT_API_DATABASE_PATH must be absolute")

    inference_mode = os.environ.get("AGENT_API_INFERENCE_MODE", "fake")
    if inference_mode == "fake":
        executor = FakeRunExecutor()
    elif inference_mode == "disabled":
        executor = None
    else:
        raise ValueError("AGENT_API_INFERENCE_MODE must be fake or disabled")

    return create_app(
        database_path=database_path,
        run_executor=executor,
        worker_shutdown_seconds=_bounded_integer(
            "AGENT_API_SHUTDOWN_SECONDS", default=10, minimum=1, maximum=30
        ),
        production_environment=bool(os.environ.get("K_SERVICE")),
        run_state_backend=(
            "firestore"
            if os.environ.get("AGENT_API_FIRESTORE_DATABASE") is not None
            else "sqlite"
        ),
        queue_backend=(
            "cloud_tasks"
            if os.environ.get("AGENT_API_FIRESTORE_DATABASE") is not None
            else "sqlite"
        ),
        queue_delivery_path=os.environ.get(
            "AGENT_API_QUEUE_DELIVERY_PATH", "/internal/tasks/run-delivery"
        ),
    )


def main() -> None:
    """Run a single bounded Uvicorn worker and let it own SIGTERM handling."""

    uvicorn.run(
        build_application(),
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
