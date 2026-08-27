from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_api.app import create_app
from agent_api.ports import RunSubmission
from agent_api.security import LimitConfig
from agent_api.storage import (
    SessionRecord,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    TenantRecord,
)

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
CORRELATION_ID = "corr-limit-client-0001"


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


@dataclass(frozen=True)
class Context:
    app: FastAPI
    client: AsyncClient
    path: Path


@asynccontextmanager
async def context(tmp_path: Path, config: LimitConfig) -> AsyncIterator[Context]:
    path = tmp_path / "limits.sqlite3"
    app = create_app(
        database_path=path,
        pepper_provider=FixedPepper(),
        clock=lambda: NOW,
        limit_config=config,
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield Context(app, client, path)


async def seed(
    current: Context,
    *,
    tenant_id: str = "tenant-one",
    session_id: str | None = None,
    scopes: tuple[str, ...] = ("runs:read", "runs:write"),
) -> str:
    await SQLiteTenantRepository(current.path).put(
        TenantRecord(tenant_id=tenant_id, created_at=NOW)
    )
    if session_id is not None:
        await SQLiteSessionRepository(current.path).put(
            SessionRecord(
                tenant_id=tenant_id,
                session_id=session_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    generated = await current.app.state.auth_manager.create(
        tenant_id=tenant_id, scopes=scopes, now=NOW
    )
    return f"Bearer {generated.plaintext}"


def headers(authorization: str, *, key: str = "request-key-one") -> dict[str, str]:
    return {
        "Authorization": authorization,
        "Idempotency-Key": key,
        "X-Correlation-ID": CORRELATION_ID,
    }


def assert_error(
    response: object,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    assert response == {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": CORRELATION_ID,
            "retryable": retryable,
            "field_issues": [],
        }
    }


@pytest.mark.asyncio
async def test_rate_limit_is_safe_exact_and_authentication_still_wins(
    tmp_path: Path,
) -> None:
    async with context(
        tmp_path, LimitConfig(request_burst=1, requests_per_second=1.0)
    ) as current:
        authorization = await seed(current)
        request_headers = {
            "Authorization": authorization,
            "X-Correlation-ID": CORRELATION_ID,
        }
        first = await current.client.get(
            "/v1/runs/run-missing", headers=request_headers
        )
        blocked = await current.client.get(
            "/v1/runs/run-missing", headers=request_headers
        )
        duplicate_auth = [
            ("Authorization", authorization),
            ("Authorization", authorization),
            ("X-Correlation-ID", CORRELATION_ID),
        ]
        unauthenticated = await current.client.get(
            "/v1/runs/run-missing", headers=duplicate_auth
        )

    assert first.status_code == 404
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "1"
    assert_error(
        blocked.json(),
        code="rate_limited",
        message="Request quota was exceeded.",
        retryable=True,
    )
    assert unauthenticated.status_code == 401


@pytest.mark.asyncio
async def test_real_body_limit_handles_chunks_content_length_lies_and_gets(
    tmp_path: Path,
) -> None:
    config = LimitConfig(max_request_bytes=64)
    async with context(tmp_path, config) as current:
        authorization = await seed(current, session_id="session-one")
        exact = b'{"query":"Find answer."}' + b" " * (64 - 24)
        accepted = await current.client.post(
            "/v1/sessions/session-one/runs",
            content=exact,
            headers={**headers(authorization), "Content-Type": "application/json"},
        )

        async def chunks() -> AsyncIterator[bytes]:
            yield b"x" * 40
            yield b"x" * 25

        oversized = await current.client.post(
            "/v1/sessions/session-one/runs",
            content=chunks(),
            headers={
                **headers(authorization, key="request-key-two"),
                "Content-Type": "application/json",
                "Content-Length": "1",
            },
        )
        get_body = await current.client.request(
            "GET",
            "/v1/runs/run-missing",
            content=b"x" * 65,
            headers={
                "Authorization": authorization,
                "X-Correlation-ID": CORRELATION_ID,
            },
        )
        bad_auth = await current.client.post(
            "/v1/sessions/session-one/runs",
            content=b"x" * 65,
            headers={
                **headers("Bearer malformed", key="request-key-three"),
                "Content-Length": "1",
            },
        )

    assert accepted.status_code == 202
    assert [oversized.status_code, get_body.status_code] == [413, 413]
    for response in (oversized, get_body):
        assert_error(
            response.json(),
            code="invalid_request",
            message="Request body was too large.",
            retryable=False,
        )
    assert bad_auth.status_code == 401


@pytest.mark.asyncio
async def test_run_quota_cannot_be_bypassed_and_releases_failed_or_terminal_slots(
    tmp_path: Path,
) -> None:
    config = LimitConfig(max_queued_runs=1, daily_work_units=10)
    async with context(tmp_path, config) as current:
        authorization = await seed(current, session_id="session-one")
        missing = await current.client.post(
            "/v1/sessions/session-missing/runs",
            json={"query": "Find the documented answer."},
            headers=headers(authorization, key="request-key-missing"),
        )
        accepted = await current.client.post(
            "/v1/sessions/session-one/runs",
            json={"query": "Find the documented answer."},
            headers=headers(authorization),
        )
        retried = await current.client.post(
            "/v1/sessions/session-one/runs",
            json={"query": "Find the documented answer."},
            headers=headers(authorization),
        )
        conflict = await current.client.post(
            "/v1/sessions/session-one/runs",
            json={"query": "Find another answer."},
            headers=headers(authorization),
        )
        blocked = await current.client.post(
            "/v1/sessions/session-one/runs",
            json={"query": "Find a second answer."},
            headers=headers(authorization, key="request-key-two"),
        )
        run_id = accepted.json()["run_id"]
        cancelled = await current.client.post(
            f"/v1/runs/{run_id}/cancel",
            headers={
                "Authorization": authorization,
                "X-Correlation-ID": CORRELATION_ID,
            },
        )
        after_cancel = await current.client.post(
            "/v1/sessions/session-one/runs",
            json={"query": "Find a second answer."},
            headers=headers(authorization, key="request-key-two"),
        )

    assert missing.status_code == 404
    assert accepted.status_code == retried.status_code == 202
    assert accepted.json()["run_id"] == retried.json()["run_id"]
    assert conflict.status_code == 409
    assert blocked.status_code == 429
    assert cancelled.status_code == 202
    assert after_cancel.status_code == 202


@pytest.mark.asyncio
async def test_sse_releases_terminal_connection_and_accounting_outage_is_503(
    tmp_path: Path,
) -> None:
    async with context(tmp_path, LimitConfig(max_sse_connections=1)) as current:
        authorization = await seed(current, session_id="session-one")
        await SQLiteRunRepository(current.path).create(
            RunSubmission(
                tenant_id="tenant-one",
                session_id="session-one",
                run_id="run-one",
                idempotency_key="request-key-one",
                query="Find the documented answer.",
                created_at=NOW,
            )
        )
        await SQLiteRunRepository(current.path).request_cancellation(
            tenant_id="tenant-one", run_id="run-one", at=NOW
        )
        stream_headers = {
            "Authorization": authorization,
            "X-Correlation-ID": CORRELATION_ID,
        }
        first = await current.client.get(
            "/v1/runs/run-one/events", headers=stream_headers
        )
        reconnected = await current.client.get(
            "/v1/runs/run-one/events", headers=stream_headers
        )
        with sqlite3.connect(current.path) as connection:
            connection.execute("DROP TABLE quota_rate_buckets")
        unavailable = await current.client.get(
            "/v1/runs/run-one", headers=stream_headers
        )

    assert first.status_code == reconnected.status_code == 200
    assert unavailable.status_code == 503
    assert_error(
        unavailable.json(),
        code="unavailable",
        message="Service is temporarily unavailable.",
        retryable=True,
    )
