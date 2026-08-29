"""Shared Firestore authority for authentication, quota, and audit state."""

from __future__ import annotations

import math
import secrets
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta

from search_agent.contracts import OpaqueId, QueryText

from ..ports import IdempotencyConflictError, IdempotencyKey
from ..storage.cloud import (
    DocumentStore,
    DocumentStoreTransaction,
    _document_id,
)
from ..storage.repositories import (
    ApiKeyHashRecord,
    AuditEntry,
    StorageConflictError,
    StorageError,
    _checked,
    _limit,
    _parse_timestamp,
    _scope_id,
    _timestamp,
)
from .limits import (
    ExecutionPermit,
    LimitConfig,
    QuotaExceeded,
    RunAdmission,
    SSEPermit,
    _request_hash,
    _seconds_until_tomorrow,
    _utc,
)

_API_KEYS = "api_key_hashes"
_AUDIT_ENTRIES = "audit_entries"
_RATE_BUCKETS = "quota_rate_buckets"
_RUN_ADMISSIONS = "quota_run_admissions"
_EXECUTION_LEASES = "quota_execution_leases"
_SSE_LEASES = "quota_sse_leases"
_QUOTA_GUARDS = "quota_guards"
_RUNS = "runs"
_IDEMPOTENCY = "idempotency_records"
_LEASE_RECLAIM_BATCH_SIZE = 5


class FirestoreApiKeyRepository:
    """Atomic key lifecycle shared by every Cloud Run replica."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    @property
    def document_store(self) -> DocumentStore:
        return self._store

    async def put(self, record: ApiKeyHashRecord) -> bool:
        checked = _checked(ApiKeyHashRecord, record)
        document_id = _document_id(checked.tenant_id, checked.key_id)

        async def operation(tx: DocumentStoreTransaction) -> bool:
            existing = _decode_key(
                await tx.get(collection=_API_KEYS, document_id=document_id),
                tenant_id=checked.tenant_id,
                key_id=checked.key_id,
            )
            if existing is not None:
                if existing != checked:
                    raise StorageConflictError("key id already exists")
                return False
            await tx.set(
                collection=_API_KEYS,
                document_id=document_id,
                document=_key_document(checked),
            )
            return True

        return await self._store.transaction(operation)

    async def get(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId
    ) -> ApiKeyHashRecord | None:
        checked_tenant = _scope_id(tenant_id)
        checked_key = _scope_id(key_id)
        return _decode_key(
            await self._store.get(
                collection=_API_KEYS,
                document_id=_document_id(checked_tenant, checked_key),
            ),
            tenant_id=checked_tenant,
            key_id=checked_key,
        )

    async def revoke(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId, at: datetime
    ) -> bool:
        checked_tenant = _scope_id(tenant_id)
        checked_key = _scope_id(key_id)
        checked_at = _utc(at)
        document_id = _document_id(checked_tenant, checked_key)

        async def operation(tx: DocumentStoreTransaction) -> bool:
            record = _decode_key(
                await tx.get(collection=_API_KEYS, document_id=document_id),
                tenant_id=checked_tenant,
                key_id=checked_key,
            )
            if record is None or record.revoked_at is not None:
                return False
            if checked_at < record.created_at:
                raise ValueError("key revocation cannot precede creation")
            revoked = record.model_copy(update={"revoked_at": checked_at})
            await tx.set(
                collection=_API_KEYS,
                document_id=document_id,
                document=_key_document(revoked),
            )
            return True

        return await self._store.transaction(operation)

    async def rotate(
        self,
        *,
        old_tenant_id: OpaqueId,
        old_key_id: OpaqueId,
        new_record: ApiKeyHashRecord,
        at: datetime,
    ) -> bool:
        checked_tenant = _scope_id(old_tenant_id)
        checked_key = _scope_id(old_key_id)
        checked_new = _checked(ApiKeyHashRecord, new_record)
        checked_at = _utc(at)
        if checked_new.tenant_id != checked_tenant:
            raise ValueError("rotated key must stay in the same tenant")
        if checked_new.rotated_from_key_id != checked_key:
            raise ValueError("rotated key must identify its predecessor")
        if checked_new.created_at != checked_at or checked_new.revoked_at is not None:
            raise ValueError("rotated key lifecycle is invalid")
        old_document_id = _document_id(checked_tenant, checked_key)
        new_document_id = _document_id(checked_new.tenant_id, checked_new.key_id)

        async def operation(tx: DocumentStoreTransaction) -> bool:
            old = _decode_key(
                await tx.get(collection=_API_KEYS, document_id=old_document_id),
                tenant_id=checked_tenant,
                key_id=checked_key,
            )
            existing_new = _decode_key(
                await tx.get(collection=_API_KEYS, document_id=new_document_id),
                tenant_id=checked_new.tenant_id,
                key_id=checked_new.key_id,
            )
            if old is None:
                return False
            if checked_at < old.created_at:
                raise ValueError("key rotation cannot precede creation")
            if old.status_at(checked_at) != "active":
                return False
            if existing_new is not None:
                raise StorageConflictError("key id already exists")
            await tx.set(
                collection=_API_KEYS,
                document_id=new_document_id,
                document=_key_document(checked_new),
            )
            await tx.set(
                collection=_API_KEYS,
                document_id=old_document_id,
                document=_key_document(
                    old.model_copy(update={"revoked_at": checked_at})
                ),
            )
            return True

        return await self._store.transaction(operation)


class FirestoreAuditRepository:
    """Append-only tenant audit records shared across replicas."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    @property
    def document_store(self) -> DocumentStore:
        return self._store

    async def append(self, entry: AuditEntry) -> bool:
        checked = _checked(AuditEntry, entry)
        document_id = _document_id(checked.tenant_id, checked.entry_id)

        async def operation(tx: DocumentStoreTransaction) -> bool:
            existing = _decode_audit(
                await tx.get(collection=_AUDIT_ENTRIES, document_id=document_id)
            )
            if existing is not None:
                if existing != checked:
                    raise StorageConflictError("audit entry id already exists")
                return False
            await tx.set(
                collection=_AUDIT_ENTRIES,
                document_id=document_id,
                document=_audit_document(checked),
            )
            return True

        return await self._store.transaction(operation)

    async def list(
        self, *, tenant_id: OpaqueId, limit: int = 100
    ) -> tuple[AuditEntry, ...]:
        rows = await self._store.list(
            collection=_AUDIT_ENTRIES,
            filters={"tenant_id": _scope_id(tenant_id)},
            order_by=("occurred_at", "entry_id"),
            limit=_limit(limit),
        )
        return tuple(_decode_required_audit(row) for row in rows)


class FirestoreQuotaLimiter:
    """Transactional shared quota accounting and expiring live-work leases."""

    def __init__(self, store: DocumentStore, config: LimitConfig) -> None:
        self._store = store
        self._config = config

    @property
    def document_store(self) -> DocumentStore:
        return self._store

    async def admit_request(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId, at: datetime
    ) -> None:
        checked_tenant = _scope_id(tenant_id)
        checked_key = _scope_id(key_id)
        now = _utc(at)
        document_id = _document_id(checked_tenant, checked_key)

        async def operation(tx: DocumentStoreTransaction) -> int | None:
            key = _decode_key(
                await tx.get(collection=_API_KEYS, document_id=document_id),
                tenant_id=checked_tenant,
                key_id=checked_key,
            )
            if key is None or key.status_at(now) != "active":
                raise StorageError("quota key is not active")
            row = await tx.get(collection=_RATE_BUCKETS, document_id=document_id)
            if row is None:
                tokens = float(self._config.request_burst)
            else:
                previous = _parse_timestamp(_require_text(row, "last_refill"))
                if now < previous:
                    raise StorageError("quota clock regressed")
                tokens = min(
                    float(self._config.request_burst),
                    _require_number(row, "tokens")
                    + (now - previous).total_seconds()
                    * self._config.requests_per_second,
                )
            allowed = tokens >= 1.0
            remaining = tokens - 1.0 if allowed else tokens
            await tx.set(
                collection=_RATE_BUCKETS,
                document_id=document_id,
                document={
                    "document_id": document_id,
                    "tenant_id": checked_tenant,
                    "key_id": checked_key,
                    "tokens": remaining,
                    "last_refill": _timestamp(now),
                },
            )
            if allowed:
                return None
            wait = (1.0 - remaining) / self._config.requests_per_second
            return max(1, math.ceil(wait))

        retry_after = await self._transaction(operation)
        if retry_after is not None:
            raise QuotaExceeded(retry_after=retry_after)

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
        checked_tenant = _scope_id(tenant_id)
        checked_key = _scope_id(key_id)
        checked_run = _scope_id(run_id)
        now = _utc(at)
        request_hash = _request_hash(session_id, query).hex()
        document_id = _document_id(checked_tenant, idempotency_key)

        async def operation(tx: DocumentStoreTransaction) -> RunAdmission:
            key = _decode_key(
                await tx.get(
                    collection=_API_KEYS,
                    document_id=_document_id(checked_tenant, checked_key),
                ),
                tenant_id=checked_tenant,
                key_id=checked_key,
            )
            if key is None or key.status_at(now) != "active":
                raise StorageError("quota key is not active")
            existing = await tx.get(collection=_RUN_ADMISSIONS, document_id=document_id)
            if existing is not None:
                if _require_text(existing, "request_hash") != request_hash:
                    raise IdempotencyConflictError
                return RunAdmission(
                    checked_tenant,
                    idempotency_key,
                    _require_text(existing, "run_id"),
                    False,
                )

            durable = await tx.get(collection=_IDEMPOTENCY, document_id=document_id)
            if durable is not None:
                if _require_text(durable, "request_hash") != request_hash:
                    raise IdempotencyConflictError
                durable_run = _require_text(durable, "run_id")
                await tx.set(
                    collection=_RUN_ADMISSIONS,
                    document_id=document_id,
                    document=_admission_document(
                        document_id=document_id,
                        tenant_id=checked_tenant,
                        key_id=checked_key,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        run_id=durable_run,
                        now=now,
                        work_units=0,
                    ),
                )
                return RunAdmission(checked_tenant, idempotency_key, durable_run, False)

            guard_id, guard = await _next_quota_guard(
                tx, tenant_id=checked_tenant, quota="run"
            )
            queued = await tx.list(
                collection=_RUNS,
                filters={"tenant_id": checked_tenant, "state": "queued"},
                limit=self._config.max_queued_runs + 1,
            )
            pending: list[tuple[str, OpaqueId, datetime]] = []
            cutoff = now - timedelta(seconds=self._config.pending_admission_seconds)
            for pending_id, pending_run, created_at in _pending_admissions(guard):
                if created_at > cutoff and (
                    await tx.get(
                        collection=_RUNS,
                        document_id=_document_id(checked_tenant, pending_run),
                    )
                    is None
                ):
                    pending.append((pending_id, pending_run, created_at))
            if len(queued) + len(pending) >= self._config.max_queued_runs:
                raise QuotaExceeded(retry_after=self._config.retry_after_seconds)

            work_day = now.date().isoformat()
            work_units = self._config.work_units_per_run
            stored_day = _optional_text(guard, "work_day")
            used = _require_int(guard, "work_units") if stored_day == work_day else 0
            if used + work_units > self._config.daily_work_units:
                raise QuotaExceeded(retry_after=_seconds_until_tomorrow(now))
            await tx.set(
                collection=_RUN_ADMISSIONS,
                document_id=document_id,
                document=_admission_document(
                    document_id=document_id,
                    tenant_id=checked_tenant,
                    key_id=checked_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    run_id=checked_run,
                    now=now,
                    work_units=work_units,
                ),
            )
            await tx.set(
                collection=_QUOTA_GUARDS,
                document_id=guard_id,
                document={
                    **guard,
                    "pending_admissions": [
                        _pending_document(*item)
                        for item in (
                            *pending,
                            (document_id, checked_run, now),
                        )
                    ],
                    "work_day": work_day,
                    "work_units": used + work_units,
                },
            )
            return RunAdmission(checked_tenant, idempotency_key, checked_run, True)

        return await self._transaction(operation)

    async def release_run(self, admission: RunAdmission) -> None:
        if not admission.created:
            return
        document_id = _document_id(admission.tenant_id, admission.idempotency_key)

        async def operation(tx: DocumentStoreTransaction) -> None:
            durable = await tx.get(collection=_IDEMPOTENCY, document_id=document_id)
            stored = await tx.get(collection=_RUN_ADMISSIONS, document_id=document_id)
            if (
                durable is None
                and stored is not None
                and _require_text(stored, "run_id") == admission.run_id
            ):
                guard_id, guard = await _next_quota_guard(
                    tx,
                    tenant_id=admission.tenant_id,
                    quota="run",
                    require_existing=True,
                )
                work_units = _require_int(stored, "work_units")
                guard_day = _optional_text(guard, "work_day")
                if guard_day is None:
                    raise StorageError("run quota ledger is missing")
                if guard_day == _require_text(stored, "work_day"):
                    used = _require_int(guard, "work_units")
                    if work_units > used:
                        raise StorageError("run quota ledger is inconsistent")
                    guard["work_units"] = used - work_units
                guard["pending_admissions"] = [
                    _pending_document(*item)
                    for item in _pending_admissions(guard)
                    if item[0] != document_id
                ]
                await tx.delete(collection=_RUN_ADMISSIONS, document_id=document_id)
                await tx.set(
                    collection=_QUOTA_GUARDS,
                    document_id=guard_id,
                    document=guard,
                )

        await self._transaction(operation)

    async def acquire_execution(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        at: datetime,
        lease_seconds: int,
    ) -> ExecutionPermit | None:
        checked_tenant = _scope_id(tenant_id)
        checked_run = _scope_id(run_id)
        now = _utc(at)
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("execution lease must be a positive integer")
        permit = ExecutionPermit(
            tenant_id=checked_tenant,
            run_id=checked_run,
            permit_id="permit-" + secrets.token_hex(16),
        )
        document_id = _document_id(checked_tenant, checked_run)

        async def operation(
            tx: DocumentStoreTransaction,
        ) -> ExecutionPermit | None:
            if await tx.get(collection=_RUNS, document_id=document_id) is None:
                raise StorageError("execution run does not exist")
            guard_id, guard = await _next_quota_guard(
                tx, tenant_id=checked_tenant, quota="execution"
            )
            rows, expired_ids, complete = await _lease_rows(
                tx,
                collection=_EXECUTION_LEASES,
                tenant_id=checked_tenant,
                now=now,
                active_limit=self._config.max_concurrent_runs + 1,
            )
            await _delete_leases(
                tx,
                collection=_EXECUTION_LEASES,
                document_ids=expired_ids,
            )
            if (
                not complete
                or any(_require_text(row, "run_id") == checked_run for row in rows)
                or len(rows) >= self._config.max_concurrent_runs
            ):
                return None
            await tx.set(
                collection=_EXECUTION_LEASES,
                document_id=document_id,
                document={
                    "document_id": document_id,
                    "tenant_id": checked_tenant,
                    "run_id": checked_run,
                    "permit_id": permit.permit_id,
                    "expires_at": _timestamp(now + timedelta(seconds=lease_seconds)),
                },
            )
            await tx.set(
                collection=_QUOTA_GUARDS,
                document_id=guard_id,
                document=guard,
            )
            return permit

        return await self._transaction(operation)

    async def release_execution(self, permit: ExecutionPermit) -> None:
        document_id = _document_id(permit.tenant_id, permit.run_id)

        async def operation(tx: DocumentStoreTransaction) -> None:
            row = await tx.get(collection=_EXECUTION_LEASES, document_id=document_id)
            if row is not None and _require_text(row, "permit_id") == permit.permit_id:
                await tx.delete(collection=_EXECUTION_LEASES, document_id=document_id)

        await self._transaction(operation)

    async def renew_execution(
        self, permit: ExecutionPermit, *, at: datetime, lease_seconds: int
    ) -> bool:
        now = _utc(at)
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("execution lease must be a positive integer")
        document_id = _document_id(permit.tenant_id, permit.run_id)

        async def operation(tx: DocumentStoreTransaction) -> bool:
            row = await tx.get(collection=_EXECUTION_LEASES, document_id=document_id)
            if (
                row is None
                or _require_text(row, "permit_id") != permit.permit_id
                or _parse_timestamp(_require_text(row, "expires_at")) <= now
            ):
                return False
            await tx.set(
                collection=_EXECUTION_LEASES,
                document_id=document_id,
                document={
                    **row,
                    "expires_at": _timestamp(now + timedelta(seconds=lease_seconds)),
                },
            )
            return True

        return await self._transaction(operation)

    async def acquire_sse(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId, at: datetime
    ) -> SSEPermit:
        checked_tenant = _scope_id(tenant_id)
        checked_key = _scope_id(key_id)
        now = _utc(at)
        permit = SSEPermit(
            tenant_id=checked_tenant,
            key_id=checked_key,
            permit_id="sse-" + secrets.token_hex(16),
        )
        document_id = _document_id(checked_tenant, permit.permit_id)

        async def operation(tx: DocumentStoreTransaction) -> SSEPermit | None:
            key = _decode_key(
                await tx.get(
                    collection=_API_KEYS,
                    document_id=_document_id(checked_tenant, checked_key),
                ),
                tenant_id=checked_tenant,
                key_id=checked_key,
            )
            if key is None or key.status_at(now) != "active":
                raise StorageError("SSE key is not active")
            guard_id, guard = await _next_quota_guard(
                tx, tenant_id=checked_tenant, quota="sse"
            )
            rows, expired_ids, complete = await _lease_rows(
                tx,
                collection=_SSE_LEASES,
                tenant_id=checked_tenant,
                now=now,
                active_limit=self._config.max_sse_connections + 1,
            )
            await _delete_leases(
                tx,
                collection=_SSE_LEASES,
                document_ids=expired_ids,
            )
            if not complete or len(rows) >= self._config.max_sse_connections:
                return None
            await tx.set(
                collection=_SSE_LEASES,
                document_id=document_id,
                document={
                    "document_id": document_id,
                    "tenant_id": checked_tenant,
                    "key_id": checked_key,
                    "permit_id": permit.permit_id,
                    "expires_at": _timestamp(
                        now + timedelta(seconds=self._config.sse_lease_seconds)
                    ),
                },
            )
            await tx.set(
                collection=_QUOTA_GUARDS,
                document_id=guard_id,
                document=guard,
            )
            return permit

        acquired = await self._transaction(operation)
        if acquired is None:
            raise QuotaExceeded(retry_after=self._config.retry_after_seconds)
        return acquired

    async def release_sse(self, permit: SSEPermit) -> None:
        document_id = _document_id(permit.tenant_id, permit.permit_id)

        async def operation(tx: DocumentStoreTransaction) -> None:
            row = await tx.get(collection=_SSE_LEASES, document_id=document_id)
            if row is not None and _require_text(row, "permit_id") == permit.permit_id:
                await tx.delete(collection=_SSE_LEASES, document_id=document_id)

        await self._transaction(operation)

    async def renew_sse(self, permit: SSEPermit, *, at: datetime) -> bool:
        now = _utc(at)
        lease_document_id = _document_id(permit.tenant_id, permit.permit_id)
        key_document_id = _document_id(permit.tenant_id, permit.key_id)

        async def operation(tx: DocumentStoreTransaction) -> bool:
            row = await tx.get(collection=_SSE_LEASES, document_id=lease_document_id)
            key = _decode_key(
                await tx.get(collection=_API_KEYS, document_id=key_document_id),
                tenant_id=permit.tenant_id,
                key_id=permit.key_id,
            )
            if (
                row is None
                or _require_text(row, "permit_id") != permit.permit_id
                or _parse_timestamp(_require_text(row, "expires_at")) <= now
                or key is None
                or key.status_at(now) != "active"
            ):
                return False
            await tx.set(
                collection=_SSE_LEASES,
                document_id=lease_document_id,
                document={
                    **row,
                    "expires_at": _timestamp(
                        now + timedelta(seconds=self._config.sse_lease_seconds)
                    ),
                },
            )
            return True

        return await self._transaction(operation)

    async def _transaction[T](
        self, operation: Callable[[DocumentStoreTransaction], Awaitable[T]]
    ) -> T:
        try:
            return await self._store.transaction(operation)
        except (IdempotencyConflictError, QuotaExceeded, StorageError, ValueError):
            raise
        except Exception as exc:
            raise StorageError("quota accounting failed") from exc


def _key_document(record: ApiKeyHashRecord) -> dict[str, object]:
    document = record.model_dump(mode="python")
    document["document_id"] = _document_id(record.tenant_id, record.key_id)
    document["scopes"] = list(record.scopes)
    document["created_at"] = _timestamp(record.created_at)
    document["expires_at"] = _stored_optional_timestamp(record.expires_at)
    document["revoked_at"] = _stored_optional_timestamp(record.revoked_at)
    return document


def _decode_key(
    document: Mapping[str, object] | None,
    *,
    tenant_id: OpaqueId,
    key_id: OpaqueId,
) -> ApiKeyHashRecord | None:
    if document is None:
        return None
    try:
        values = dict(document)
        document_id = _require_text(values, "document_id")
        values.pop("document_id")
        values["created_at"] = _parse_timestamp(_require_text(document, "created_at"))
        values["expires_at"] = _optional_timestamp(document, "expires_at")
        values["revoked_at"] = _optional_timestamp(document, "revoked_at")
        record = ApiKeyHashRecord.model_validate(values)
        if (
            record.tenant_id != tenant_id
            or record.key_id != key_id
            or document_id != _document_id(tenant_id, key_id)
        ):
            raise StorageError("stored API key scope is inconsistent")
        return record
    except (TypeError, ValueError) as exc:
        raise StorageError("stored API key failed validation") from exc


async def _lease_rows(
    tx: DocumentStoreTransaction,
    *,
    collection: str,
    tenant_id: OpaqueId,
    now: datetime,
    active_limit: int,
) -> tuple[tuple[dict[str, object], ...], tuple[str, ...], bool]:
    scan_limit = active_limit + _LEASE_RECLAIM_BATCH_SIZE
    rows = await tx.list(
        collection=collection,
        document_id_prefix=_document_id(tenant_id, ""),
        limit=scan_limit,
    )
    active: list[dict[str, object]] = []
    expired_ids: list[str] = []
    for row in rows:
        document_id = _lease_document_id(
            row, collection=collection, tenant_id=tenant_id
        )
        if _parse_timestamp(_require_text(row, "expires_at")) <= now:
            if len(expired_ids) >= _LEASE_RECLAIM_BATCH_SIZE:
                continue
            expired_ids.append(document_id)
        else:
            active.append(row)
    return tuple(active), tuple(expired_ids), len(rows) < scan_limit


def _lease_document_id(
    row: Mapping[str, object], *, collection: str, tenant_id: OpaqueId
) -> str:
    if collection == _EXECUTION_LEASES:
        identity_field = "run_id"
    elif collection == _SSE_LEASES:
        identity_field = "permit_id"
    else:
        raise ValueError("unsupported lease collection")
    document_id = _require_text(row, "document_id")
    if _require_text(row, "tenant_id") != tenant_id or document_id != _document_id(
        tenant_id, _require_text(row, identity_field)
    ):
        raise StorageError("stored lease scope is inconsistent")
    return document_id


async def _delete_leases(
    tx: DocumentStoreTransaction,
    *,
    collection: str,
    document_ids: tuple[str, ...],
) -> None:
    for document_id in document_ids:
        await tx.delete_known(collection=collection, document_id=document_id)


def _audit_document(entry: AuditEntry) -> dict[str, object]:
    document = entry.model_dump(mode="python")
    document["document_id"] = _document_id(entry.tenant_id, entry.entry_id)
    document["occurred_at"] = _timestamp(entry.occurred_at)
    return document


def _decode_audit(document: Mapping[str, object] | None) -> AuditEntry | None:
    if document is None:
        return None
    try:
        values = dict(document)
        values.pop("document_id", None)
        values["occurred_at"] = _parse_timestamp(_require_text(document, "occurred_at"))
        return AuditEntry.model_validate(values)
    except ValueError as exc:
        raise StorageError("stored audit entry failed validation") from exc


def _decode_required_audit(document: Mapping[str, object]) -> AuditEntry:
    entry = _decode_audit(document)
    assert entry is not None
    return entry


def _admission_document(
    *,
    document_id: str,
    tenant_id: OpaqueId,
    key_id: OpaqueId,
    idempotency_key: IdempotencyKey,
    request_hash: str,
    run_id: OpaqueId,
    now: datetime,
    work_units: int,
) -> dict[str, object]:
    return {
        "document_id": document_id,
        "tenant_id": tenant_id,
        "key_id": key_id,
        "idempotency_key": idempotency_key,
        "request_hash": request_hash,
        "run_id": run_id,
        "work_day": now.date().isoformat(),
        "work_units": work_units,
        "created_at": _timestamp(now),
    }


async def _next_quota_guard(
    tx: DocumentStoreTransaction,
    *,
    tenant_id: OpaqueId,
    quota: str,
    require_existing: bool = False,
) -> tuple[str, dict[str, object]]:
    """Read the common document that makes distinct admission writes conflict."""
    document_id = _document_id(tenant_id, quota)
    row = await tx.get(collection=_QUOTA_GUARDS, document_id=document_id)
    if row is None:
        if require_existing:
            raise StorageError("quota guard is missing")
        return document_id, {
            "document_id": document_id,
            "tenant_id": tenant_id,
            "quota": quota,
            "version": 1,
        }
    if (
        _require_text(row, "tenant_id") != tenant_id
        or _require_text(row, "quota") != quota
    ):
        raise StorageError("quota guard scope is inconsistent")
    return document_id, {**row, "version": _require_int(row, "version") + 1}


def _pending_admissions(
    guard: Mapping[str, object],
) -> tuple[tuple[str, OpaqueId, datetime], ...]:
    stored = guard.get("pending_admissions", [])
    if type(stored) is not list:
        raise StorageError("pending admissions are not stored as a list")
    pending: list[tuple[str, OpaqueId, datetime]] = []
    for value in stored:
        if type(value) is not dict:
            raise StorageError("pending admission is not stored as an object")
        pending.append(
            (
                _require_text(value, "document_id"),
                _require_text(value, "run_id"),
                _parse_timestamp(_require_text(value, "created_at")),
            )
        )
    return tuple(pending)


def _pending_document(
    document_id: str, run_id: OpaqueId, created_at: datetime
) -> dict[str, object]:
    return {
        "document_id": document_id,
        "run_id": run_id,
        "created_at": _timestamp(created_at),
    }


def _require_text(document: Mapping[str, object], field: str) -> str:
    value = document.get(field)
    if type(value) is not str:
        raise StorageError(f"{field} is not stored as text")
    return value


def _optional_text(document: Mapping[str, object], field: str) -> str | None:
    value = document.get(field)
    if value is not None and type(value) is not str:
        raise StorageError(f"{field} is not stored as optional text")
    return value


def _require_int(document: Mapping[str, object], field: str) -> int:
    value = document.get(field)
    if type(value) is not int:
        raise StorageError(f"{field} is not stored as an integer")
    return value


def _require_number(document: Mapping[str, object], field: str) -> float:
    value = document.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StorageError(f"{field} is not stored as a number")
    number = float(value)
    if not math.isfinite(number):
        raise StorageError(f"{field} is not stored as a finite number")
    return number


def _optional_timestamp(document: Mapping[str, object], field: str) -> datetime | None:
    value = document.get(field)
    return None if value is None else _parse_timestamp(value)


def _stored_optional_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


__all__ = [
    "FirestoreApiKeyRepository",
    "FirestoreAuditRepository",
    "FirestoreQuotaLimiter",
]
