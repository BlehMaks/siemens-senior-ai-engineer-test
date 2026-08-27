from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import agent_api.observability as observability_module
from agent_api.app import create_app
from agent_api.observability import OperationalTelemetry, SQLiteReadinessProbe
from agent_api.ports import RunFailureCode, RunState
from agent_api.storage import (
    AuditEntry,
    SessionRecord,
    SQLiteAuditRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    TenantRecord,
    migrate,
)
from search_agent import RunUsage

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class AuditRecorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[AuditEntry] = []
        self.fail = fail

    async def append(self, entry: AuditEntry) -> bool:
        if self.fail:
            raise RuntimeError("hostile audit diagnostic must stay private")
        self.entries.append(entry)
        return True


class ExplodingLogger(logging.Logger):
    def info(self, msg: object, *args: object, **kwargs: object) -> None:
        del msg, args, kwargs
        raise RuntimeError("hostile logging diagnostic must stay private")


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


def usage() -> RunUsage:
    return RunUsage(
        elapsed_seconds=1.25,
        iterations=2,
        search_queries=1,
        pages=3,
        failed_pages=1,
        raw_bytes_reserved=4096,
        decoded_bytes=2048,
        model_calls=1,
        model_attempts=2,
        tokens=512,
    )


@asynccontextmanager
async def api_client(path: Path) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    app = create_app(
        database_path=path,
        pepper_provider=FixedPepper(),
        clock=lambda: NOW,
        run_id_factory=lambda: "run-observe-one",
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield app, client


@pytest.mark.asyncio
async def test_terminal_log_is_diagnostic_but_contains_no_raw_identity_or_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit = AuditRecorder()
    logger = logging.getLogger("agent_api.operations.test")
    logger.setLevel(logging.INFO)
    telemetry = OperationalTelemetry(
        pseudonym_key=b"k" * 32,
        audit=audit,
        logger=logger,
    )
    tenant = "tenant-private-pii-0001"
    session = "session-secret-prompt-0001"
    run = "run-raw-evidence-0001"

    with caplog.at_level(logging.INFO, logger=logger.name):
        await telemetry.run_terminal(
            tenant_id=tenant,
            session_id=session,
            run_id=run,
            state=RunState.FAILED,
            failure_code=RunFailureCode.SEARCH_FAILED,
            usage=usage(),
            at=NOW,
        )

    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].message)
    assert payload == {
        "decoded_bytes": 2048,
        "elapsed_seconds": 1.25,
        "event": "run.terminal",
        "failed_pages": 1,
        "failure": "search_failed",
        "iterations": 2,
        "model_attempts": 2,
        "model_calls": 1,
        "occurred_at": "2026-08-27T10:00:00.000000+00:00",
        "pages": 3,
        "raw_bytes_reserved": 4096,
        "run": payload["run"],
        "search_queries": 1,
        "session": payload["session"],
        "state": "failed",
        "tenant": payload["tenant"],
        "tokens": 512,
    }
    assert payload["tenant"].startswith("tenant-")
    assert payload["session"].startswith("session-")
    assert payload["run"].startswith("run-")
    serialized = caplog.records[0].message
    for forbidden in (tenant, session, run, "private", "secret", "prompt", "evidence"):
        assert forbidden not in serialized
    assert [(entry.tenant_id, entry.action) for entry in audit.entries] == [
        (tenant, "run.failed")
    ]

    samples = telemetry.snapshot()
    assert any(
        sample.name == "api_runs_terminal_total"
        and sample.labels == (("failure", "search_failed"), ("state", "failed"))
        for sample in samples
    )
    assert all(
        raw not in str(samples)
        for raw in (tenant, session, run, "corr-private-client-0001")
    )


@pytest.mark.asyncio
async def test_telemetry_failures_are_fail_open_and_have_bounded_labels() -> None:
    telemetry = OperationalTelemetry(
        pseudonym_key=b"k" * 32,
        audit=AuditRecorder(fail=True),
        logger=ExplodingLogger("exploding"),
    )

    await telemetry.run_submitted(
        tenant_id="tenant-one",
        session_id="session-one",
        run_id="run-one",
        correlation_id="corr-client-one",
        at=NOW,
    )
    telemetry.work_outcome(
        tenant_id="tenant-one", run_id="run-one", outcome="claimed", at=NOW
    )
    with pytest.raises(ValueError, match="bounded"):
        telemetry.work_outcome(
            tenant_id="tenant-one",
            run_id="run-one",
            outcome="run-one",
            at=NOW,
        )

    samples = telemetry.snapshot()
    assert {
        sample.labels
        for sample in samples
        if sample.name == "api_telemetry_failures_total"
    } == {
        (("component", "audit"),),
        (("component", "logging"),),
    }
    assert all("tenant" not in dict(sample.labels) for sample in samples)


@pytest.mark.asyncio
async def test_run_routes_emit_joinable_safe_logs_and_idempotent_audit(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "route-observability.sqlite3"
    async with api_client(path) as (app, client):
        await SQLiteTenantRepository(path).put(
            TenantRecord(tenant_id="tenant-private-one", created_at=NOW)
        )
        await SQLiteSessionRepository(path).put(
            SessionRecord(
                tenant_id="tenant-private-one",
                session_id="session-private-one",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        generated = await app.state.auth_manager.create(
            tenant_id="tenant-private-one",
            scopes=("runs:read", "runs:write"),
            now=NOW,
        )
        headers = {
            "Authorization": f"Bearer {generated.plaintext}",
            "Idempotency-Key": "request-observe-one",
            "X-Correlation-ID": "corr-observe-client-one",
        }
        with caplog.at_level(logging.INFO, logger="agent_api.operations"):
            accepted = await client.post(
                "/v1/sessions/session-private-one/runs",
                json={"query": "prompt evidence https://private.example.invalid"},
                headers=headers,
            )
            cancelled = await client.post(
                "/v1/runs/run-observe-one/cancel",
                headers={
                    "Authorization": headers["Authorization"],
                    "X-Correlation-ID": headers["X-Correlation-ID"],
                },
            )

        assert accepted.status_code == cancelled.status_code == 202
        payloads = [json.loads(record.message) for record in caplog.records]
        assert [payload["event"] for payload in payloads] == [
            "run.submitted",
            "run.cancellation_requested",
        ]
        assert {payload["correlation_id"] for payload in payloads} == {
            "corr-observe-client-one"
        }
        serialized = "\n".join(record.message for record in caplog.records)
        for forbidden in (
            "tenant-private-one",
            "session-private-one",
            "run-observe-one",
            generated.plaintext,
            "prompt",
            "evidence",
            "private.example",
        ):
            assert forbidden not in serialized
        audit = await SQLiteAuditRepository(path).list(tenant_id="tenant-private-one")
        assert {entry.action for entry in audit} == {
            "run.cancelled",
            "run.submitted",
            "run.cancellation-requested",
        }
        assert all(entry.entry_id.startswith("audit-") for entry in audit)
        assert all(
            "tenant" not in dict(sample.labels)
            for sample in app.state.telemetry.snapshot()
        )


@pytest.mark.asyncio
async def test_sqlite_readiness_is_read_only_and_checks_runtime_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ready.sqlite3"
    probe = SQLiteReadinessProbe(path)
    assert not await probe.ready()

    await migrate(path)
    assert await probe.ready()

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE quota_rate_buckets")
    assert not await probe.ready()

    replacement = tmp_path / "lookalike.sqlite3"
    with sqlite3.connect(replacement) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (placeholder INTEGER);
            CREATE TABLE runs (placeholder INTEGER);
            CREATE TABLE work_items (placeholder INTEGER);
            CREATE TABLE quota_rate_buckets (placeholder INTEGER);
            """
        )
    replacement.replace(path)
    assert not await probe.ready()


@pytest.mark.asyncio
async def test_sqlite_readiness_rejects_replacement_during_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    await migrate(path)
    probe = SQLiteReadinessProbe(path)
    opened = asyncio.Event()
    resume = asyncio.Event()
    original = observability_module.validate_current_schema

    async def delayed_validation(connection: aiosqlite.Connection) -> None:
        opened.set()
        await resume.wait()
        await original(connection)

    monkeypatch.setattr(
        observability_module, "validate_current_schema", delayed_validation
    )
    checking = asyncio.create_task(probe.ready())
    await asyncio.wait_for(opened.wait(), timeout=1)
    replacement = tmp_path / "replacement.sqlite3"
    with sqlite3.connect(replacement):
        pass
    replacement.replace(path)
    resume.set()

    assert not await asyncio.wait_for(checking, timeout=1)


@pytest.mark.asyncio
async def test_sqlite_readiness_rejects_symlink_swap_during_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    await migrate(path)
    same_inode = tmp_path / "same-inode.sqlite3"
    same_inode.hardlink_to(path)
    probe = SQLiteReadinessProbe(path)
    opened = asyncio.Event()
    resume = asyncio.Event()
    original = observability_module.validate_current_schema

    async def delayed_validation(connection: aiosqlite.Connection) -> None:
        opened.set()
        await resume.wait()
        await original(connection)

    monkeypatch.setattr(
        observability_module, "validate_current_schema", delayed_validation
    )
    checking = asyncio.create_task(probe.ready())
    await asyncio.wait_for(opened.wait(), timeout=1)
    path.unlink()
    path.symlink_to(same_inode)
    resume.set()

    assert not await asyncio.wait_for(checking, timeout=1)


@pytest.mark.asyncio
async def test_metric_totals_saturate_at_a_finite_value() -> None:
    telemetry = OperationalTelemetry(
        pseudonym_key=b"k" * 32,
        audit=AuditRecorder(),
    )
    huge = usage().model_copy(update={"elapsed_seconds": 1e308})

    for run_id in ("run-one", "run-two", "run-three"):
        await telemetry.run_terminal(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id=run_id,
            state=RunState.COMPLETED,
            failure_code=None,
            usage=huge,
            at=NOW,
        )

    elapsed = next(
        sample
        for sample in telemetry.snapshot()
        if sample.name == "api_run_elapsed_seconds_total"
    )
    assert math.isfinite(elapsed.value)
    assert elapsed.value >= 1e308


@pytest.mark.parametrize("key", [b"", b"k" * 31, bytearray(b"k" * 32)])
def test_telemetry_rejects_weak_or_non_byte_pseudonym_keys(key: object) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        OperationalTelemetry(
            pseudonym_key=key,  # type: ignore[arg-type]
            audit=AuditRecorder(),
        )
