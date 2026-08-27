from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agent_api.app import create_app
from agent_api.observability import ReadinessProbe

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


class ExplodingProbe:
    async def ready(self) -> bool:
        raise RuntimeError("private dependency detail")


@asynccontextmanager
async def client_for(
    path: Path, *, readiness_probe: ReadinessProbe | None = None
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        database_path=path,
        pepper_provider=FixedPepper(),
        clock=lambda: NOW,
        readiness_probe=readiness_probe,
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield client


@pytest.mark.asyncio
async def test_liveness_and_readiness_are_public_bounded_contracts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "health.sqlite3"
    async with client_for(path) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE quota_rate_buckets")
        unavailable = await client.get("/health/ready")
        still_live = await client.get("/health/live")

    assert live.status_code == ready.status_code == still_live.status_code == 200
    assert live.headers["x-correlation-id"].startswith("corr-")
    assert unavailable.headers["x-correlation-id"].startswith("corr-")
    assert (
        live.json()
        == ready.json()
        == still_live.json()
        == {
            "status": "ok",
            "checked_at": "2026-08-27T10:00:00Z",
        }
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "status": "not_ready",
        "checked_at": "2026-08-27T10:00:00Z",
    }
    assert "quota" not in unavailable.text


@pytest.mark.asyncio
async def test_readiness_probe_exception_is_a_safe_not_ready_response(
    tmp_path: Path,
) -> None:
    async with client_for(
        tmp_path / "health-error.sqlite3", readiness_probe=ExplodingProbe()
    ) as client:
        response = await client.get(
            "/health/ready", headers={"X-Correlation-ID": "corr-health-client-one"}
        )

    assert response.status_code == 503
    assert response.headers["x-correlation-id"] == "corr-health-client-one"
    assert response.json()["status"] == "not_ready"
    assert "private" not in response.text
