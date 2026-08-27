"""Small, fail-closed quota boundary for authenticated API work."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Protocol, cast

import aiosqlite
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from search_agent.contracts import OpaqueId, QueryText

from ..ports import IdempotencyConflictError, IdempotencyKey
from ..storage import StorageError


@dataclass(frozen=True, slots=True)
class LimitConfig:
    max_request_bytes: int = 16 * 1024
    request_burst: int = 1_000
    requests_per_second: float = 1_000.0
    max_queued_runs: int = 100
    max_concurrent_runs: int = 4
    max_sse_connections: int = 4
    daily_work_units: int = 100
    work_units_per_run: int = 1
    sse_lease_seconds: int = 30
    pending_admission_seconds: int = 30
    retry_after_seconds: int = 1

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_request_bytes,
            self.request_burst,
            self.max_sse_connections,
            self.sse_lease_seconds,
            self.pending_admission_seconds,
            self.retry_after_seconds,
        )
        if any(type(value) is not int or value <= 0 for value in integer_limits):
            raise ValueError("positive quota limits must be exact integers")
        if self.sse_lease_seconds < 30:
            raise ValueError("SSE lease must cover two heartbeat intervals")
        bounded_counts = (
            self.max_queued_runs,
            self.max_concurrent_runs,
            self.daily_work_units,
            self.work_units_per_run,
        )
        if any(type(value) is not int or value < 0 for value in bounded_counts):
            raise ValueError("quota counts must be non-negative exact integers")
        rate = self.requests_per_second
        if (
            isinstance(rate, bool)
            or not isinstance(rate, int | float)
            or not math.isfinite(rate)
            or rate <= 0
        ):
            raise ValueError("request rate must be positive and finite")


class QuotaExceeded(RuntimeError):
    def __init__(self, *, retry_after: int) -> None:
        if type(retry_after) is not int or retry_after <= 0:
            raise ValueError("Retry-After must be a positive integer")
        self.retry_after = retry_after
        super().__init__("quota exceeded")


class RequestTooLarge(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RunAdmission:
    tenant_id: OpaqueId
    idempotency_key: IdempotencyKey
    run_id: OpaqueId
    created: bool


@dataclass(frozen=True, slots=True)
class ExecutionPermit:
    tenant_id: OpaqueId
    run_id: OpaqueId
    permit_id: str


@dataclass(frozen=True, slots=True)
class SSEPermit:
    tenant_id: OpaqueId
    key_id: OpaqueId
    permit_id: str


class QuotaLimiter(Protocol):
    async def admit_request(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId, at: datetime
    ) -> None: ...

    async def admit_run(
        self,
        *,
        tenant_id: OpaqueId,
        key_id: OpaqueId,
        session_id: OpaqueId,
        idempotency_key: IdempotencyKey,
        query: QueryText,
        run_id: OpaqueId,
        at: datetime,
    ) -> RunAdmission: ...

    async def release_run(self, admission: RunAdmission) -> None: ...

    async def acquire_execution(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        at: datetime,
        lease_seconds: int,
    ) -> ExecutionPermit | None: ...

    async def release_execution(self, permit: ExecutionPermit) -> None: ...

    async def renew_execution(
        self, permit: ExecutionPermit, *, at: datetime, lease_seconds: int
    ) -> bool: ...

    async def acquire_sse(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId, at: datetime
    ) -> SSEPermit: ...

    async def release_sse(self, permit: SSEPermit) -> None: ...

    async def renew_sse(self, permit: SSEPermit, *, at: datetime) -> bool: ...


class RequestBodyLimitMiddleware:
    """Boundedly pre-read and replay every HTTP body for authenticated enforcement."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        body = bytearray()
        too_large = False
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if type(chunk) is not bytes or len(body) + len(chunk) > self._max_bytes:
                too_large = True
                break
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        state = scope.setdefault("state", {})
        state["agent_api.request_too_large"] = too_large
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            if disconnected:
                return {"type": "http.disconnect"}
            replay_body = b"{}" if too_large else bytes(body)
            return {"type": "http.request", "body": replay_body, "more_body": False}

        await self._app(scope, replay, send)


class SQLiteQuotaLimiter:
    """SQLite-backed durable admission and expiring live-work leases."""

    def __init__(self, path: Path, config: LimitConfig) -> None:
        if not isinstance(path, Path) or path.is_symlink() or not path.is_file():
            raise StorageError("SQLite path must be a regular file")
        self._path = path
        self._config = config

    async def admit_request(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId, at: datetime
    ) -> None:
        now = _utc(at)
        async with _write_connection(self._path) as connection:
            row = await (
                await connection.execute(
                    "SELECT tokens, last_refill FROM quota_rate_buckets "
                    "WHERE tenant_id = ? AND key_id = ?",
                    (tenant_id, key_id),
                )
            ).fetchone()
            if row is None:
                tokens = float(self._config.request_burst)
            else:
                previous = _parse_timestamp(row[1])
                if now < previous:
                    raise StorageError("quota clock regressed")
                tokens = min(
                    float(self._config.request_burst),
                    float(row[0])
                    + (now - previous).total_seconds()
                    * self._config.requests_per_second,
                )
            allowed = tokens >= 1.0
            remaining = tokens - 1.0 if allowed else tokens
            await connection.execute(
                "INSERT INTO quota_rate_buckets "
                "(tenant_id, key_id, tokens, last_refill) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, key_id) DO UPDATE SET "
                "tokens = excluded.tokens, last_refill = excluded.last_refill",
                (tenant_id, key_id, remaining, _timestamp(now)),
            )
            if not allowed:
                wait = (1.0 - remaining) / self._config.requests_per_second
                raise QuotaExceeded(retry_after=max(1, math.ceil(wait)))

    async def admit_run(
        self,
        *,
        tenant_id: OpaqueId,
        key_id: OpaqueId,
        session_id: OpaqueId,
        idempotency_key: IdempotencyKey,
        query: QueryText,
        run_id: OpaqueId,
        at: datetime,
    ) -> RunAdmission:
        now = _utc(at)
        request_hash = _request_hash(session_id, query)
        async with _write_connection(self._path) as connection:
            existing = await (
                await connection.execute(
                    "SELECT request_hash, run_id FROM quota_run_admissions "
                    "WHERE tenant_id = ? AND idempotency_key = ?",
                    (tenant_id, idempotency_key),
                )
            ).fetchone()
            if existing is not None:
                if existing[0] != request_hash:
                    raise IdempotencyConflictError
                return RunAdmission(
                    tenant_id, idempotency_key, cast(str, existing[1]), False
                )

            durable = await (
                await connection.execute(
                    "SELECT request_hash, run_id FROM idempotency_records "
                    "WHERE tenant_id = ? AND idempotency_key = ?",
                    (tenant_id, idempotency_key),
                )
            ).fetchone()
            if durable is not None:
                if durable[0] != request_hash:
                    raise IdempotencyConflictError
                await self._insert_admission(
                    connection,
                    tenant_id=tenant_id,
                    key_id=key_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    run_id=cast(str, durable[1]),
                    now=now,
                    work_units=0,
                )
                return RunAdmission(
                    tenant_id, idempotency_key, cast(str, durable[1]), False
                )

            queued_row = await (
                await connection.execute(
                    "SELECT COUNT(*) FROM runs "
                    "WHERE tenant_id = ? AND state = 'queued'",
                    (tenant_id,),
                )
            ).fetchone()
            assert queued_row is not None
            queued = cast(
                int,
                queued_row[0],
            )
            pending_row = await (
                await connection.execute(
                    "SELECT COUNT(*) FROM quota_run_admissions AS q "
                    "LEFT JOIN runs AS r ON r.tenant_id = q.tenant_id "
                    "AND r.run_id = q.run_id "
                    "WHERE q.tenant_id = ? AND r.run_id IS NULL "
                    "AND q.created_at > ?",
                    (
                        tenant_id,
                        _timestamp(
                            now
                            - timedelta(seconds=self._config.pending_admission_seconds)
                        ),
                    ),
                )
            ).fetchone()
            assert pending_row is not None
            pending = cast(
                int,
                pending_row[0],
            )
            if queued + pending >= self._config.max_queued_runs:
                raise QuotaExceeded(retry_after=self._config.retry_after_seconds)

            work_day = now.date().isoformat()
            used_row = await (
                await connection.execute(
                    "SELECT COALESCE(SUM(work_units), 0) "
                    "FROM quota_run_admissions "
                    "WHERE tenant_id = ? AND work_day = ?",
                    (tenant_id, work_day),
                )
            ).fetchone()
            assert used_row is not None
            used = cast(
                int,
                used_row[0],
            )
            if used + self._config.work_units_per_run > self._config.daily_work_units:
                raise QuotaExceeded(retry_after=_seconds_until_tomorrow(now))
            await self._insert_admission(
                connection,
                tenant_id=tenant_id,
                key_id=key_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                run_id=run_id,
                now=now,
                work_units=self._config.work_units_per_run,
            )
            return RunAdmission(tenant_id, idempotency_key, run_id, True)

    async def _insert_admission(
        self,
        connection: aiosqlite.Connection,
        *,
        tenant_id: OpaqueId,
        key_id: OpaqueId,
        idempotency_key: IdempotencyKey,
        request_hash: bytes,
        run_id: OpaqueId,
        now: datetime,
        work_units: int,
    ) -> None:
        await connection.execute(
            "INSERT INTO quota_run_admissions "
            "(tenant_id, key_id, idempotency_key, request_hash, run_id, work_day, "
            "work_units, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                key_id,
                idempotency_key,
                request_hash,
                run_id,
                now.date().isoformat(),
                work_units,
                _timestamp(now),
            ),
        )

    async def release_run(self, admission: RunAdmission) -> None:
        if not admission.created:
            return
        async with _write_connection(self._path) as connection:
            durable = await (
                await connection.execute(
                    "SELECT 1 FROM idempotency_records "
                    "WHERE tenant_id = ? AND idempotency_key = ?",
                    (admission.tenant_id, admission.idempotency_key),
                )
            ).fetchone()
            if durable is None:
                await connection.execute(
                    "DELETE FROM quota_run_admissions WHERE tenant_id = ? "
                    "AND idempotency_key = ? AND run_id = ?",
                    (
                        admission.tenant_id,
                        admission.idempotency_key,
                        admission.run_id,
                    ),
                )

    async def acquire_execution(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        at: datetime,
        lease_seconds: int,
    ) -> ExecutionPermit | None:
        now = _utc(at)
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("execution lease must be a positive integer")
        permit_id = "permit-" + secrets.token_hex(16)
        async with _write_connection(self._path) as connection:
            await connection.execute(
                "DELETE FROM quota_execution_leases "
                "WHERE tenant_id = ? AND expires_at <= ?",
                (tenant_id, _timestamp(now)),
            )
            existing = await (
                await connection.execute(
                    "SELECT 1 FROM quota_execution_leases "
                    "WHERE tenant_id = ? AND run_id = ?",
                    (tenant_id, run_id),
                )
            ).fetchone()
            count_row = await (
                await connection.execute(
                    "SELECT COUNT(*) FROM quota_execution_leases WHERE tenant_id = ?",
                    (tenant_id,),
                )
            ).fetchone()
            assert count_row is not None
            count = cast(
                int,
                count_row[0],
            )
            if existing is not None or count >= self._config.max_concurrent_runs:
                return None
            expires_at = now + timedelta(seconds=lease_seconds)
            await connection.execute(
                "INSERT INTO quota_execution_leases "
                "(tenant_id, run_id, permit_id, expires_at) VALUES (?, ?, ?, ?)",
                (tenant_id, run_id, permit_id, _timestamp(expires_at)),
            )
        return ExecutionPermit(tenant_id, run_id, permit_id)

    async def release_execution(self, permit: ExecutionPermit) -> None:
        async with _write_connection(self._path) as connection:
            await connection.execute(
                "DELETE FROM quota_execution_leases WHERE tenant_id = ? "
                "AND run_id = ? AND permit_id = ?",
                (permit.tenant_id, permit.run_id, permit.permit_id),
            )

    async def renew_execution(
        self, permit: ExecutionPermit, *, at: datetime, lease_seconds: int
    ) -> bool:
        now = _utc(at)
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("execution lease must be a positive integer")
        expires_at = now + timedelta(seconds=lease_seconds)
        async with _write_connection(self._path) as connection:
            cursor = await connection.execute(
                "UPDATE quota_execution_leases SET expires_at = ? "
                "WHERE tenant_id = ? AND run_id = ? AND permit_id = ?",
                (
                    _timestamp(expires_at),
                    permit.tenant_id,
                    permit.run_id,
                    permit.permit_id,
                ),
            )
            return cursor.rowcount == 1

    async def acquire_sse(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId, at: datetime
    ) -> SSEPermit:
        now = _utc(at)
        permit_id = "sse-" + secrets.token_hex(16)
        async with _write_connection(self._path) as connection:
            await connection.execute(
                "DELETE FROM quota_sse_leases WHERE tenant_id = ? AND expires_at <= ?",
                (tenant_id, _timestamp(now)),
            )
            count_row = await (
                await connection.execute(
                    "SELECT COUNT(*) FROM quota_sse_leases WHERE tenant_id = ?",
                    (tenant_id,),
                )
            ).fetchone()
            assert count_row is not None
            count = cast(
                int,
                count_row[0],
            )
            if count >= self._config.max_sse_connections:
                raise QuotaExceeded(retry_after=self._config.retry_after_seconds)
            await connection.execute(
                "INSERT INTO quota_sse_leases "
                "(tenant_id, key_id, permit_id, expires_at) VALUES (?, ?, ?, ?)",
                (
                    tenant_id,
                    key_id,
                    permit_id,
                    _timestamp(now + timedelta(seconds=self._config.sse_lease_seconds)),
                ),
            )
        return SSEPermit(tenant_id, key_id, permit_id)

    async def release_sse(self, permit: SSEPermit) -> None:
        async with _write_connection(self._path) as connection:
            await connection.execute(
                "DELETE FROM quota_sse_leases WHERE tenant_id = ? "
                "AND key_id = ? AND permit_id = ?",
                (permit.tenant_id, permit.key_id, permit.permit_id),
            )

    async def renew_sse(self, permit: SSEPermit, *, at: datetime) -> bool:
        now = _utc(at)
        async with _write_connection(self._path) as connection:
            cursor = await connection.execute(
                "UPDATE quota_sse_leases SET expires_at = ? "
                "WHERE tenant_id = ? AND key_id = ? AND permit_id = ?",
                (
                    _timestamp(now + timedelta(seconds=self._config.sse_lease_seconds)),
                    permit.tenant_id,
                    permit.key_id,
                    permit.permit_id,
                ),
            )
            return cursor.rowcount == 1


@asynccontextmanager
async def _write_connection(path: Path) -> AsyncIterator[aiosqlite.Connection]:
    try:
        async with aiosqlite.connect(path, isolation_level=None) as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()
    except (QuotaExceeded, IdempotencyConflictError, StorageError):
        raise
    except sqlite3.Error as exc:
        raise StorageError("quota accounting failed") from exc


def _request_hash(session_id: str, query: str) -> bytes:
    return hashlib.sha256(
        json.dumps(
            [session_id, query], ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).digest()


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("quota timestamp must be UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError("quota timestamp must be UTC")
    return value


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _parse_timestamp(value: object) -> datetime:
    try:
        if type(value) is not str:
            raise ValueError
        return _utc(datetime.fromisoformat(value))
    except (TypeError, ValueError) as exc:
        raise StorageError("stored quota timestamp is invalid") from exc


def _seconds_until_tomorrow(now: datetime) -> int:
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time(), tzinfo=UTC)
    return max(1, math.ceil((tomorrow - now).total_seconds()))


def request_too_large(request: Request) -> bool:
    return request.scope.get("state", {}).get("agent_api.request_too_large") is True


__all__ = [
    "ExecutionPermit",
    "LimitConfig",
    "QuotaExceeded",
    "QuotaLimiter",
    "RequestBodyLimitMiddleware",
    "RequestTooLarge",
    "RunAdmission",
    "SQLiteQuotaLimiter",
    "SSEPermit",
    "request_too_large",
]
