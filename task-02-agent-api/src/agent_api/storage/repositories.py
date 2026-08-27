"""Concrete SQLite repositories with tenant predicates on every data access."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import aiosqlite
from pydantic import (
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from search_agent.contracts import OpaqueId, ScopedAnswer, StrictModel
from search_agent.memory import SQLiteReflectionRepository

from ..ports import (
    TERMINAL_RUN_STATES,
    CancellationResult,
    ClaimDisposition,
    ClaimRequest,
    ClaimResult,
    CreateRunResult,
    ExecutionLease,
    IdempotencyConflictError,
    LeaseDisposition,
    LeaseRenewal,
    LeaseResult,
    RunRecord,
    RunState,
    RunSubmission,
    StateUpdate,
    StateUpdateResult,
    WriteDisposition,
)
from ..schemas import RunEvent, RunFailure, SessionLabel

DisplayName = Annotated[
    str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)
]
KeyHash = Annotated[bytes, Field(min_length=32, max_length=128, repr=False)]
AuditAction = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=80,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    ),
]
ApiKeyScope = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?::[a-z][a-z0-9]*)*$",
    ),
]

_OPAQUE_ID = TypeAdapter(OpaqueId)
_MAX_SCOPES_TEXT = 2048


class StorageError(RuntimeError):
    """A safe persistence failure without SQL, row contents, or credentials."""


class StorageConflictError(ValueError):
    """A stable identifier was reused for different immutable content."""


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise ValueError("timestamp must be an exact datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


def _scope_id(value: OpaqueId) -> OpaqueId:
    if type(value) is not str:
        raise ValueError("scope id must be a string")
    return _OPAQUE_ID.validate_python(value, strict=True)


def _checked[ModelT: StrictModel](model_type: type[ModelT], value: ModelT) -> ModelT:
    if type(value) is not model_type:
        raise ValueError("storage value has the wrong concrete type")
    return model_type.model_validate(value.model_dump(mode="python"))


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("limit must be between 1 and 100")
    return value


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


@asynccontextmanager
async def _connection(
    path: Path, *, write: bool = False
) -> AsyncIterator[aiosqlite.Connection]:
    try:
        async with aiosqlite.connect(path, isolation_level=None) as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 5000")
            if write:
                # BEGIN IMMEDIATE serializes writers before they read CAS inputs.
                await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                if write:
                    await connection.rollback()
                raise
            else:
                if write:
                    await connection.commit()
    except StorageError:
        raise
    except sqlite3.Error as exc:
        raise StorageError("SQLite operation failed") from exc


class _PathRepository:
    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise StorageError("SQLite path must be a filesystem path")
        if path.is_symlink() or not path.is_file():
            raise StorageError("SQLite path must be a regular file")
        self._path = path


class TenantRecord(StrictModel):
    tenant_id: OpaqueId
    display_name: DisplayName | None = None
    created_at: datetime

    _created_at_is_utc = field_validator("created_at")(_utc)


class ApiKeyHashRecord(StrictModel):
    """Only a derived hash crosses this boundary; plaintext keys have no field."""

    tenant_id: OpaqueId = Field(repr=False)
    key_id: OpaqueId = Field(repr=False)
    key_hash: KeyHash
    scopes: tuple[ApiKeyScope, ...] = Field(default=(), repr=False)
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    rotated_from_key_id: OpaqueId | None = Field(default=None, repr=False)

    _created_at_is_utc = field_validator("created_at")(_utc)
    _optional_timestamps_are_utc = field_validator("expires_at", "revoked_at")(
        _optional_utc
    )

    @field_validator("key_hash", mode="before")
    @classmethod
    def require_bytes(cls, value: object) -> object:
        if type(value) is not bytes:
            raise ValueError("key hash must be exact bytes")
        return value

    @field_validator("scopes", mode="before")
    @classmethod
    def normalize_scopes(cls, value: object) -> object:
        if isinstance(value, list):
            value = tuple(value)
        if type(value) is not tuple:
            raise ValueError("scopes must be an exact tuple")
        if len(value) > 64:
            raise ValueError("too many key scopes")
        if any(type(scope) is not str for scope in value):
            raise ValueError("scopes must be exact strings")
        normalized = tuple(sorted(set(value)))
        if len(json.dumps(normalized, separators=(",", ":"))) > _MAX_SCOPES_TEXT:
            raise ValueError("serialized key scopes exceed their limit")
        return normalized

    @model_validator(mode="after")
    def validate_lifecycle(self) -> ApiKeyHashRecord:
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("key expiry must follow creation")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("key revocation cannot precede creation")
        if self.rotated_from_key_id == self.key_id:
            raise ValueError("key cannot rotate from itself")
        return self

    def status_at(self, now: datetime) -> str:
        checked_now = _utc(now)
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= checked_now:
            return "expired"
        return "active"


class SessionRecord(StrictModel):
    tenant_id: OpaqueId
    session_id: OpaqueId
    label: SessionLabel | None = None
    created_at: datetime
    updated_at: datetime

    _timestamps_are_utc = field_validator("created_at", "updated_at")(_utc)

    @model_validator(mode="after")
    def validate_timestamps(self) -> SessionRecord:
        if self.updated_at < self.created_at:
            raise ValueError("session update cannot precede creation")
        return self


class AuditEntry(StrictModel):
    tenant_id: OpaqueId
    entry_id: OpaqueId
    action: AuditAction
    occurred_at: datetime

    _occurred_at_is_utc = field_validator("occurred_at")(_utc)


class SQLiteTenantRepository(_PathRepository):
    async def put(self, tenant: TenantRecord) -> bool:
        checked = _checked(TenantRecord, tenant)
        async with _connection(self._path, write=True) as connection:
            row = await (
                await connection.execute(
                    "SELECT display_name, created_at FROM tenants WHERE tenant_id = ?",
                    (checked.tenant_id,),
                )
            ).fetchone()
            expected = (checked.display_name, _timestamp(checked.created_at))
            if row is not None:
                if tuple(row) != expected:
                    raise StorageConflictError("tenant id already exists")
                return False
            await connection.execute(
                "INSERT INTO tenants (tenant_id, display_name, created_at) "
                "VALUES (?, ?, ?)",
                (checked.tenant_id, *expected),
            )
            return True

    async def get(self, *, tenant_id: OpaqueId) -> TenantRecord | None:
        checked_tenant = _scope_id(tenant_id)
        async with _connection(self._path) as connection:
            row = await (
                await connection.execute(
                    "SELECT tenant_id, display_name, created_at FROM tenants "
                    "WHERE tenant_id = ?",
                    (checked_tenant,),
                )
            ).fetchone()
        return (
            None
            if row is None
            else TenantRecord(
                tenant_id=row[0],
                display_name=row[1],
                created_at=_parse_timestamp(row[2]),
            )
        )

    async def delete(self, *, tenant_id: OpaqueId) -> bool:
        checked_tenant = _scope_id(tenant_id)
        async with _connection(self._path, write=True) as connection:
            # The imported Task 1 table intentionally has its exact standalone schema,
            # so its tenant rows are removed explicitly rather than by a foreign key.
            await connection.execute(
                "DELETE FROM run_reflections WHERE tenant_id = ?", (checked_tenant,)
            )
            cursor = await connection.execute(
                "DELETE FROM tenants WHERE tenant_id = ?", (checked_tenant,)
            )
            return cursor.rowcount == 1


class SQLiteKeyHashRepository(_PathRepository):
    async def put(self, record: ApiKeyHashRecord) -> bool:
        checked = _checked(ApiKeyHashRecord, record)
        values = _key_values(checked)
        async with _connection(self._path, write=True) as connection:
            row = await (
                await connection.execute(
                    "SELECT key_hash, scopes, created_at, expires_at, revoked_at, "
                    "rotated_from_key_id "
                    "FROM api_key_hashes WHERE tenant_id = ? AND key_id = ?",
                    (checked.tenant_id, checked.key_id),
                )
            ).fetchone()
            if row is not None:
                if tuple(row) != values:
                    raise StorageConflictError("key id already exists")
                return False
            await connection.execute(
                "INSERT INTO api_key_hashes "
                "(tenant_id, key_id, key_hash, scopes, created_at, expires_at, "
                "revoked_at, rotated_from_key_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (checked.tenant_id, checked.key_id, *values),
            )
            return True

    async def get(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId
    ) -> ApiKeyHashRecord | None:
        scope = (_scope_id(tenant_id), _scope_id(key_id))
        async with _connection(self._path) as connection:
            row = await (
                await connection.execute(
                    "SELECT tenant_id, key_id, key_hash, scopes, created_at, "
                    "expires_at, revoked_at, rotated_from_key_id FROM api_key_hashes "
                    "WHERE tenant_id = ? AND key_id = ?",
                    scope,
                )
            ).fetchone()
        return None if row is None else _decode_key(row)

    async def revoke(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId, at: datetime
    ) -> bool:
        scope = (_scope_id(tenant_id), _scope_id(key_id))
        checked_at = _timestamp(at)
        async with _connection(self._path, write=True) as connection:
            row = await (
                await connection.execute(
                    "SELECT created_at, revoked_at FROM api_key_hashes "
                    "WHERE tenant_id = ? AND key_id = ?",
                    scope,
                )
            ).fetchone()
            if row is None or row[1] is not None:
                return False
            if at < _parse_timestamp(row[0]):
                raise ValueError("key revocation cannot precede creation")
            cursor = await connection.execute(
                "UPDATE api_key_hashes SET revoked_at = ? "
                "WHERE tenant_id = ? AND key_id = ? AND revoked_at IS NULL",
                (checked_at, *scope),
            )
            return cursor.rowcount == 1

    async def rotate(
        self,
        *,
        old_tenant_id: OpaqueId,
        old_key_id: OpaqueId,
        new_record: ApiKeyHashRecord,
        at: datetime,
    ) -> bool:
        checked_old = (_scope_id(old_tenant_id), _scope_id(old_key_id))
        checked_new = _checked(ApiKeyHashRecord, new_record)
        if checked_new.tenant_id != checked_old[0]:
            raise ValueError("rotated key must stay in the same tenant")
        if checked_new.rotated_from_key_id != checked_old[1]:
            raise ValueError("rotated key must identify its predecessor")
        checked_at = _timestamp(at)
        if checked_new.created_at != at or checked_new.revoked_at is not None:
            raise ValueError("rotated key lifecycle is invalid")
        values = _key_values(checked_new)
        async with _connection(self._path, write=True) as connection:
            old = await (
                await connection.execute(
                    "SELECT created_at, expires_at, revoked_at FROM api_key_hashes "
                    "WHERE tenant_id = ? AND key_id = ?",
                    checked_old,
                )
            ).fetchone()
            if old is None:
                return False
            if at < _parse_timestamp(old[0]):
                raise ValueError("key rotation cannot precede creation")
            if old[2] is not None:
                return False
            if old[1] is not None and _parse_timestamp(old[1]) <= at:
                return False
            await connection.execute(
                "INSERT INTO api_key_hashes "
                "(tenant_id, key_id, key_hash, scopes, created_at, expires_at, "
                "revoked_at, rotated_from_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (checked_new.tenant_id, checked_new.key_id, *values),
            )
            cursor = await connection.execute(
                "UPDATE api_key_hashes SET revoked_at = ? "
                "WHERE tenant_id = ? AND key_id = ? AND revoked_at IS NULL",
                (checked_at, *checked_old),
            )
            return cursor.rowcount == 1


class SQLiteSessionRepository(_PathRepository):
    async def put(self, session: SessionRecord) -> bool:
        checked = _checked(SessionRecord, session)
        values = (
            checked.label,
            _timestamp(checked.created_at),
            _timestamp(checked.updated_at),
        )
        async with _connection(self._path, write=True) as connection:
            row = await (
                await connection.execute(
                    "SELECT label, created_at, updated_at FROM sessions "
                    "WHERE tenant_id = ? AND session_id = ?",
                    (checked.tenant_id, checked.session_id),
                )
            ).fetchone()
            if row is not None:
                if tuple(row) != values:
                    raise StorageConflictError("session id already exists")
                return False
            await connection.execute(
                "INSERT INTO sessions "
                "(tenant_id, session_id, label, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (checked.tenant_id, checked.session_id, *values),
            )
            return True

    async def get(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId
    ) -> SessionRecord | None:
        scope = (_scope_id(tenant_id), _scope_id(session_id))
        async with _connection(self._path) as connection:
            row = await (
                await connection.execute(
                    "SELECT tenant_id, session_id, label, created_at, updated_at "
                    "FROM sessions WHERE tenant_id = ? AND session_id = ?",
                    scope,
                )
            ).fetchone()
        return None if row is None else _decode_session(row)

    async def list(
        self, *, tenant_id: OpaqueId, limit: int = 100
    ) -> tuple[SessionRecord, ...]:
        checked_tenant = _scope_id(tenant_id)
        checked_limit = _limit(limit)
        async with _connection(self._path) as connection:
            rows = await (
                await connection.execute(
                    "SELECT tenant_id, session_id, label, created_at, updated_at "
                    "FROM sessions WHERE tenant_id = ? "
                    "ORDER BY created_at, session_id LIMIT ?",
                    (checked_tenant, checked_limit),
                )
            ).fetchall()
        return tuple(_decode_session(row) for row in rows)

    async def delete(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> bool:
        scope = (_scope_id(tenant_id), _scope_id(session_id))
        async with _connection(self._path, write=True) as connection:
            await connection.execute(
                "DELETE FROM run_reflections WHERE tenant_id = ? AND session_id = ?",
                scope,
            )
            cursor = await connection.execute(
                "DELETE FROM sessions WHERE tenant_id = ? AND session_id = ?", scope
            )
            return cursor.rowcount == 1


class SQLiteRunRepository(_PathRepository):
    """Durable API-00 state machine; every write is one SQLite transaction."""

    async def create(self, submission: RunSubmission) -> CreateRunResult:
        checked = _checked(RunSubmission, submission)
        request_hash = hashlib.sha256(
            json.dumps(
                [checked.session_id, checked.query],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).digest()
        async with _connection(self._path, write=True) as connection:
            existing = await (
                await connection.execute(
                    "SELECT request_hash, run_id FROM idempotency_records "
                    "WHERE tenant_id = ? AND idempotency_key = ?",
                    (checked.tenant_id, checked.idempotency_key),
                )
            ).fetchone()
            if existing is not None:
                if existing[0] != request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key already identifies another request"
                    )
                run = await _get_run(connection, checked.tenant_id, existing[1])
                if run is None:
                    raise StorageError("idempotency record has no run")
                return CreateRunResult(run=run, created=False)
            if await _get_run(connection, checked.tenant_id, checked.run_id):
                raise IdempotencyConflictError("run id already exists")

            timestamp = _timestamp(checked.created_at)
            await connection.execute(
                "INSERT OR IGNORE INTO tenants (tenant_id, display_name, created_at) "
                "VALUES (?, NULL, ?)",
                (checked.tenant_id, timestamp),
            )
            await connection.execute(
                "INSERT OR IGNORE INTO sessions "
                "(tenant_id, session_id, label, created_at, updated_at) "
                "VALUES (?, ?, NULL, ?, ?)",
                (checked.tenant_id, checked.session_id, timestamp, timestamp),
            )
            run = RunRecord(
                **checked.model_dump(mode="python", exclude={"created_at"}),
                state=RunState.QUEUED,
                version=0,
                delivery_attempts=0,
                created_at=checked.created_at,
                updated_at=checked.created_at,
            )
            await _insert_run(connection, run)
            await connection.execute(
                "INSERT INTO idempotency_records "
                "(tenant_id, idempotency_key, request_hash, run_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    checked.tenant_id,
                    checked.idempotency_key,
                    request_hash,
                    checked.run_id,
                    timestamp,
                ),
            )
            return CreateRunResult(run=run, created=True)

    async def get(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> RunRecord | None:
        scope = (_scope_id(tenant_id), _scope_id(run_id))
        async with _connection(self._path) as connection:
            return await _get_run(connection, *scope)

    async def list_session(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, limit: int = 100
    ) -> tuple[RunRecord, ...]:
        checked_tenant = _scope_id(tenant_id)
        checked_session = _scope_id(session_id)
        checked_limit = _limit(limit)
        async with _connection(self._path) as connection:
            rows = await (
                await connection.execute(
                    _RUN_SELECT + " WHERE tenant_id = ? AND session_id = ? "
                    "ORDER BY created_at, run_id LIMIT ?",
                    (checked_tenant, checked_session, checked_limit),
                )
            ).fetchall()
        return tuple(_decode_run(row) for row in rows)

    async def claim(self, request: ClaimRequest) -> ClaimResult:
        checked = _checked(ClaimRequest, request)
        async with _connection(self._path, write=True) as connection:
            run = await _get_run(connection, checked.tenant_id, checked.run_id)
            if run is None:
                return ClaimResult(disposition=ClaimDisposition.NOT_FOUND, run=None)
            if run.state in TERMINAL_RUN_STATES:
                return ClaimResult(disposition=ClaimDisposition.TERMINAL, run=run)
            if checked.now < run.updated_at:
                raise ValueError("claim time cannot precede the stored update")
            if run.cancellation_requested_at is not None:
                if run.lease is not None and run.lease.expires_at > checked.now:
                    return ClaimResult(
                        disposition=ClaimDisposition.CANCELLATION_REQUESTED, run=run
                    )
                cancelled = _changed_run(
                    run,
                    state=RunState.CANCELLED,
                    updated_at=checked.now,
                    terminal_at=checked.now,
                    lease=None,
                )
                await _save_run(connection, run, cancelled)
                return ClaimResult(
                    disposition=ClaimDisposition.CANCELLATION_REQUESTED,
                    run=cancelled,
                )
            if run.lease is not None and run.lease.expires_at > checked.now:
                exact_owner = (
                    run.lease.lease_id == checked.lease_id
                    and run.lease.worker_id == checked.worker_id
                )
                return ClaimResult(
                    disposition=(
                        ClaimDisposition.ALREADY_CLAIMED
                        if exact_owner
                        else ClaimDisposition.BUSY
                    ),
                    run=run,
                )
            expires_at = _lease_expiry(checked.now, checked.lease_seconds)
            if expires_at is None:
                return ClaimResult(
                    disposition=ClaimDisposition.LEASE_UNAVAILABLE, run=run
                )
            claimed = _changed_run(
                run,
                state=RunState.RUNNING,
                updated_at=checked.now,
                delivery_attempts=run.delivery_attempts + 1,
                lease=ExecutionLease(
                    lease_id=checked.lease_id,
                    worker_id=checked.worker_id,
                    acquired_at=checked.now,
                    expires_at=expires_at,
                ),
            )
            await _save_run(connection, run, claimed)
            return ClaimResult(disposition=ClaimDisposition.CLAIMED, run=claimed)

    async def renew_lease(self, renewal: LeaseRenewal) -> LeaseResult:
        checked = _checked(LeaseRenewal, renewal)
        async with _connection(self._path, write=True) as connection:
            run = await _get_run(connection, checked.tenant_id, checked.run_id)
            if run is None:
                return LeaseResult(disposition=LeaseDisposition.NOT_FOUND, run=None)
            if run.state in TERMINAL_RUN_STATES:
                return LeaseResult(disposition=LeaseDisposition.TERMINAL, run=run)
            if run.cancellation_requested_at is not None:
                return LeaseResult(
                    disposition=LeaseDisposition.CANCELLATION_REQUESTED, run=run
                )
            lease = run.lease
            if (
                lease is None
                or lease.lease_id != checked.lease_id
                or lease.worker_id != checked.worker_id
                or lease.expires_at <= checked.now
                or checked.now < run.updated_at
            ):
                return LeaseResult(disposition=LeaseDisposition.LOST, run=run)
            requested_expiry = _lease_expiry(checked.now, checked.lease_seconds)
            if requested_expiry is None:
                return LeaseResult(
                    disposition=LeaseDisposition.LEASE_UNAVAILABLE, run=run
                )
            renewed = _changed_run(
                run,
                updated_at=checked.now,
                lease=ExecutionLease(
                    lease_id=lease.lease_id,
                    worker_id=lease.worker_id,
                    acquired_at=lease.acquired_at,
                    expires_at=max(lease.expires_at, requested_expiry),
                ),
            )
            await _save_run(connection, run, renewed)
            return LeaseResult(disposition=LeaseDisposition.RENEWED, run=renewed)

    async def compare_and_set(self, update: StateUpdate) -> StateUpdateResult:
        checked = _checked(StateUpdate, update)
        async with _connection(self._path, write=True) as connection:
            run = await _get_run(connection, checked.tenant_id, checked.run_id)
            if run is None:
                return StateUpdateResult(
                    disposition=WriteDisposition.NOT_FOUND, run=None
                )
            if (
                run.version != checked.expected_version
                or run.state is not checked.expected_state
                or checked.at < run.updated_at
            ):
                return StateUpdateResult(disposition=WriteDisposition.CONFLICT, run=run)
            if checked.lease_id is not None and (
                run.lease is None
                or run.lease.lease_id != checked.lease_id
                or run.lease.worker_id != checked.worker_id
                or run.lease.expires_at <= checked.at
            ):
                return StateUpdateResult(
                    disposition=WriteDisposition.LEASE_LOST, run=run
                )
            if (
                run.cancellation_requested_at is not None
                and checked.next_state is not RunState.CANCELLED
            ):
                return StateUpdateResult(
                    disposition=WriteDisposition.CANCELLATION_REQUESTED, run=run
                )
            terminal = checked.next_state in TERMINAL_RUN_STATES
            changed = _changed_run(
                run,
                state=checked.next_state,
                updated_at=checked.at,
                terminal_at=checked.at if terminal else None,
                lease=None if terminal else run.lease,
            )
            if not await _save_run(connection, run, changed):
                current = await _get_run(connection, checked.tenant_id, checked.run_id)
                return StateUpdateResult(
                    disposition=WriteDisposition.CONFLICT, run=current
                )
            return StateUpdateResult(disposition=WriteDisposition.APPLIED, run=changed)

    async def request_cancellation(
        self, *, tenant_id: OpaqueId, run_id: OpaqueId, at: datetime
    ) -> CancellationResult:
        scope = (_scope_id(tenant_id), _scope_id(run_id))
        checked_at = _utc(at)
        async with _connection(self._path, write=True) as connection:
            run = await _get_run(connection, *scope)
            if run is None:
                return CancellationResult(run=None, changed=False)
            if run.state in TERMINAL_RUN_STATES or run.cancellation_requested_at:
                return CancellationResult(run=run, changed=False)
            if checked_at < run.updated_at:
                raise ValueError("cancellation time cannot precede the stored update")
            immediate = run.state is RunState.QUEUED or (
                run.lease is not None and run.lease.expires_at <= checked_at
            )
            cancelled = _changed_run(
                run,
                state=RunState.CANCELLED if immediate else run.state,
                updated_at=checked_at,
                cancellation_requested_at=checked_at,
                terminal_at=checked_at if immediate else None,
                lease=None if immediate else run.lease,
            )
            await _save_run(connection, run, cancelled)
            return CancellationResult(run=cancelled, changed=True)

    async def delete_run(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> bool:
        scope = (_scope_id(tenant_id), _scope_id(run_id))
        async with _connection(self._path, write=True) as connection:
            cursor = await connection.execute(
                "DELETE FROM runs WHERE tenant_id = ? AND run_id = ?", scope
            )
            return cursor.rowcount == 1

    async def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int:
        scope = (_scope_id(tenant_id), _scope_id(session_id))
        async with _connection(self._path, write=True) as connection:
            cursor = await connection.execute(
                "DELETE FROM runs WHERE tenant_id = ? AND session_id = ?", scope
            )
            return cursor.rowcount

    async def delete_tenant(self, *, tenant_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        async with _connection(self._path, write=True) as connection:
            cursor = await connection.execute(
                "DELETE FROM runs WHERE tenant_id = ?", (checked_tenant,)
            )
            return cursor.rowcount


class SQLiteEventRepository(_PathRepository):
    async def append(self, *, tenant_id: OpaqueId, event: RunEvent) -> bool:
        checked_tenant = _scope_id(tenant_id)
        checked = _checked(RunEvent, event)
        payload = checked.model_dump_json(exclude_none=True)
        async with _connection(self._path, write=True) as connection:
            row = await (
                await connection.execute(
                    "SELECT payload FROM run_events "
                    "WHERE tenant_id = ? AND run_id = ? AND sequence = ?",
                    (checked_tenant, checked.run_id, checked.sequence),
                )
            ).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise StorageConflictError("event sequence already exists")
                return False
            if await _get_run(connection, checked_tenant, checked.run_id) is None:
                raise StorageConflictError("event run does not exist")
            await connection.execute(
                "INSERT INTO run_events "
                "(tenant_id, run_id, sequence, occurred_at, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    checked_tenant,
                    checked.run_id,
                    checked.sequence,
                    _timestamp(checked.occurred_at),
                    payload,
                ),
            )
            return True

    async def list(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[RunEvent, ...]:
        scope = (_scope_id(tenant_id), _scope_id(run_id))
        if type(after_sequence) is not int or not after_sequence >= 0:
            raise ValueError("event sequence must be a non-negative integer")
        checked_limit = _limit(limit)
        async with _connection(self._path) as connection:
            rows = await (
                await connection.execute(
                    "SELECT tenant_id, run_id, sequence, occurred_at, payload "
                    "FROM run_events WHERE tenant_id = ? AND run_id = ? "
                    "AND sequence > ? ORDER BY sequence LIMIT ?",
                    (*scope, after_sequence, checked_limit),
                )
            ).fetchall()
        return tuple(_decode_event(row) for row in rows)


class SQLiteAuditRepository(_PathRepository):
    async def append(self, entry: AuditEntry) -> bool:
        checked = _checked(AuditEntry, entry)
        values = (checked.action, _timestamp(checked.occurred_at))
        async with _connection(self._path, write=True) as connection:
            row = await (
                await connection.execute(
                    "SELECT action, occurred_at FROM audit_entries "
                    "WHERE tenant_id = ? AND entry_id = ?",
                    (checked.tenant_id, checked.entry_id),
                )
            ).fetchone()
            if row is not None:
                if tuple(row) != values:
                    raise StorageConflictError("audit entry id already exists")
                return False
            await connection.execute(
                "INSERT INTO audit_entries "
                "(tenant_id, entry_id, action, occurred_at) VALUES (?, ?, ?, ?)",
                (checked.tenant_id, checked.entry_id, *values),
            )
            return True

    async def list(
        self, *, tenant_id: OpaqueId, limit: int = 100
    ) -> tuple[AuditEntry, ...]:
        checked_tenant = _scope_id(tenant_id)
        checked_limit = _limit(limit)
        async with _connection(self._path) as connection:
            rows = await (
                await connection.execute(
                    "SELECT tenant_id, entry_id, action, occurred_at "
                    "FROM audit_entries WHERE tenant_id = ? "
                    "ORDER BY occurred_at, entry_id LIMIT ?",
                    (checked_tenant, checked_limit),
                )
            ).fetchall()
        return tuple(
            AuditEntry(
                tenant_id=row[0],
                entry_id=row[1],
                action=row[2],
                occurred_at=_parse_timestamp(row[3]),
            )
            for row in rows
        )


def reflection_repository(path: Path) -> SQLiteReflectionRepository:
    """Return Task 1's exact adapter after :func:`migrate` created its table."""

    return SQLiteReflectionRepository(path)


_RUN_SELECT = (
    "SELECT tenant_id, run_id, session_id, state, version, created_at, payload "
    "FROM runs"
)


async def _get_run(
    connection: aiosqlite.Connection, tenant_id: str, run_id: str
) -> RunRecord | None:
    row = await (
        await connection.execute(
            _RUN_SELECT + " WHERE tenant_id = ? AND run_id = ?",
            (tenant_id, run_id),
        )
    ).fetchone()
    return None if row is None else _decode_run(row)


def _decode_run(row: Sequence[object]) -> RunRecord:
    try:
        payload_text = row[6]
        if type(payload_text) is not str:
            raise ValueError("run payload is not text")
        payload = json.loads(payload_text)
        if type(payload) is not dict:
            raise ValueError("run payload is not an object")
        for field in (
            "created_at",
            "updated_at",
            "cancellation_requested_at",
            "terminal_at",
        ):
            if payload.get(field) is not None:
                payload[field] = _parse_timestamp(payload[field])
        lease = payload.get("lease")
        if lease is not None:
            if type(lease) is not dict:
                raise ValueError("run lease is not an object")
            lease["acquired_at"] = _parse_timestamp(lease.get("acquired_at"))
            lease["expires_at"] = _parse_timestamp(lease.get("expires_at"))
        parsed = RunRecord.model_validate(payload, strict=False)
        run = RunRecord.model_validate(parsed.model_dump(mode="python"))
        expected = (
            run.tenant_id,
            run.run_id,
            run.session_id,
            run.state.value,
            str(run.version),
            _timestamp(run.created_at),
        )
        if tuple(row[:6]) != expected:
            raise ValueError("indexed run fields disagree with payload")
        return run
    except (TypeError, ValueError) as exc:
        raise StorageError("stored run failed validation") from exc


async def _insert_run(connection: aiosqlite.Connection, run: RunRecord) -> None:
    await connection.execute(
        "INSERT INTO runs "
        "(tenant_id, run_id, session_id, state, version, created_at, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run.tenant_id,
            run.run_id,
            run.session_id,
            run.state.value,
            str(run.version),
            _timestamp(run.created_at),
            run.model_dump_json(),
        ),
    )


async def _save_run(
    connection: aiosqlite.Connection, previous: RunRecord, changed: RunRecord
) -> bool:
    cursor = await connection.execute(
        "UPDATE runs SET state = ?, version = ?, payload = ? "
        "WHERE tenant_id = ? AND run_id = ? AND version = ? AND state = ?",
        (
            changed.state.value,
            str(changed.version),
            changed.model_dump_json(),
            previous.tenant_id,
            previous.run_id,
            str(previous.version),
            previous.state.value,
        ),
    )
    return cursor.rowcount == 1


def _changed_run(run: RunRecord, **updates: object) -> RunRecord:
    changed = run.model_copy(update={"version": run.version + 1, **updates})
    return RunRecord.model_validate(changed.model_dump(mode="python"))


def _lease_expiry(now: datetime, lease_seconds: int) -> datetime | None:
    delta = timedelta(seconds=lease_seconds)
    last_instant = datetime.max.replace(tzinfo=now.tzinfo)
    return None if last_instant - now < delta else now + delta


def _key_values(record: ApiKeyHashRecord) -> tuple[object, ...]:
    return (
        record.key_hash,
        json.dumps(record.scopes, separators=(",", ":")),
        _timestamp(record.created_at),
        None if record.expires_at is None else _timestamp(record.expires_at),
        None if record.revoked_at is None else _timestamp(record.revoked_at),
        record.rotated_from_key_id,
    )


def _decode_key(row: Sequence[object]) -> ApiKeyHashRecord:
    try:
        scopes_text = row[3]
        if type(scopes_text) is not str:
            raise ValueError("stored key scopes are not text")
        scopes = json.loads(scopes_text)
        if type(scopes) is not list or any(type(scope) is not str for scope in scopes):
            raise ValueError("stored key scopes are not a string list")
        return ApiKeyHashRecord.model_validate(
            {
                "tenant_id": row[0],
                "key_id": row[1],
                "key_hash": row[2],
                "scopes": tuple(scopes),
                "created_at": _parse_timestamp(row[4]),
                "expires_at": None if row[5] is None else _parse_timestamp(row[5]),
                "revoked_at": None if row[6] is None else _parse_timestamp(row[6]),
                "rotated_from_key_id": row[7],
            }
        )
    except (TypeError, ValueError):
        raise StorageError("stored key hash failed validation") from None


def _decode_session(row: Sequence[object]) -> SessionRecord:
    return SessionRecord.model_validate(
        {
            "tenant_id": row[0],
            "session_id": row[1],
            "label": row[2],
            "created_at": _parse_timestamp(row[3]),
            "updated_at": _parse_timestamp(row[4]),
        }
    )


def _decode_event(row: Sequence[object]) -> RunEvent:
    try:
        payload_text = row[4]
        if type(payload_text) is not str:
            raise ValueError("event payload is not text")
        payload = json.loads(payload_text)
        if type(payload) is not dict:
            raise ValueError("event payload is not an object")
        payload["occurred_at"] = _parse_timestamp(payload.get("occurred_at"))
        if payload.get("answer") is not None:
            payload["answer"] = ScopedAnswer.model_validate_json(
                json.dumps(payload["answer"], ensure_ascii=False)
            )
        if payload.get("failure") is not None:
            payload["failure"] = RunFailure.model_validate_json(
                json.dumps(payload["failure"], ensure_ascii=False)
            )
        parsed = RunEvent.model_validate(payload, strict=False)
        event = RunEvent.model_validate(parsed.model_dump(mode="python"))
        if tuple(row[1:4]) != (
            event.run_id,
            event.sequence,
            _timestamp(event.occurred_at),
        ):
            raise ValueError("indexed event fields disagree with payload")
        return event
    except (TypeError, ValueError) as exc:
        raise StorageError("stored event failed validation") from exc


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("stored timestamp is not text")
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


__all__ = [
    "ApiKeyHashRecord",
    "ApiKeyScope",
    "AuditEntry",
    "SQLiteAuditRepository",
    "SQLiteEventRepository",
    "SQLiteKeyHashRepository",
    "SQLiteRunRepository",
    "SQLiteSessionRepository",
    "SQLiteTenantRepository",
    "SessionRecord",
    "StorageConflictError",
    "StorageError",
    "TenantRecord",
    "reflection_repository",
]
