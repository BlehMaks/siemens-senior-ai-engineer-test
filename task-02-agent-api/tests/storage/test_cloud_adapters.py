from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import AnyHttpUrl
from test_ports_contract import (
    NOW,
    RunRepositoryContract,
    WorkQueueContract,
    submission,
)

from agent_api.app import create_app
from agent_api.observability import OperationalTelemetry
from agent_api.ports import (
    ClaimRequest,
    RunParentNotFoundError,
    RunRepository,
    RunState,
    WorkItem,
    WorkQueue,
)
from agent_api.schemas import RunEvent, RunEventType
from agent_api.security import (
    ApiKeyAuthError,
    ApiKeyManager,
    FirestoreApiKeyRepository,
    FirestoreAuditRepository,
    FirestoreQuotaLimiter,
    LimitConfig,
    QuotaExceeded,
    RunAdmission,
    SQLiteQuotaLimiter,
)
from agent_api.storage import (
    AuditEntry,
    CloudTask,
    CloudTaskAlreadyExistsError,
    CloudTasksWorkQueue,
    DocumentStoreTransaction,
    FirestoreEventRepository,
    FirestoreRunRepository,
    FirestoreSessionRepository,
    SessionRecord,
    SignedWorkItemCodec,
    SQLiteAuditRepository,
    SQLiteEventRepository,
    SQLiteRunRepository,
    SQLiteWorkQueue,
    StorageConflictError,
    StorageError,
    TaskDeliveryAuthError,
)
from agent_api.workers.local import LocalWorker
from search_agent import (
    Citation,
    ExtractedEvidence,
    PublicEvent,
    QueryPlan,
    RunResult,
    RunStateGraph,
    RunUsage,
    ScopedAnswer,
    SearchHit,
    SearchQuery,
    ToolBudget,
)
from search_agent.contracts import EventType, OpaqueId, QueryText


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


class CompletedExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def run(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        request: QueryText,
    ) -> RunResult:
        self.calls.append((tenant_id, session_id, run_id, str(request)))
        snapshot = RunStateGraph.create(tenant_id, session_id, run_id, str(request))
        created = PublicEvent(
            tenant_id=snapshot.tenant_id,
            session_id=snapshot.session_id,
            run_id=snapshot.run_id,
            event_type=EventType.RUN_CREATED,
            message="Created bounded research run",
        )
        plan = QueryPlan(
            tool_budget=ToolBudget(max_search_queries=1, max_fetches=1),
            searches=(SearchQuery(text="Siemens official report", max_results=1),),
        )
        snapshot, planned = RunStateGraph.accept_plan(snapshot, plan)
        snapshot, started = RunStateGraph.start_search(snapshot)
        report_url = AnyHttpUrl("https://www.siemens.com/reports/sustainability-2025")
        hit = SearchHit(
            title="Siemens report",
            url=report_url,
            snippet="Official public report",
            rank=1,
        )
        evidence = ExtractedEvidence(
            evidence_id="ev-report",
            source_url=report_url,
            source_title="Siemens report",
            summary="Siemens published its 2025 sustainability report.",
            quotes=("Siemens published its 2025 sustainability report.",),
        )
        snapshot, recorded = RunStateGraph.record_evidence(
            snapshot, hits=(hit,), evidence=(evidence,)
        )
        answer = ScopedAnswer(
            answer_text="Siemens published its 2025 sustainability report.",
            citations=(
                Citation(
                    claim="Siemens published its 2025 sustainability report.",
                    evidence_id="ev-report",
                    source_url=report_url,
                ),
            ),
        )
        snapshot, drafted = RunStateGraph.draft_answer(snapshot, answer)
        snapshot, completed = RunStateGraph.complete(snapshot)
        return RunResult(
            snapshot=snapshot,
            events=(created, planned, started, recorded, drafted, completed),
            usage=RunUsage(
                elapsed_seconds=0.5,
                iterations=4,
                search_queries=1,
                pages=1,
                failed_pages=0,
                raw_bytes_reserved=128,
                decoded_bytes=64,
                model_calls=1,
                model_attempts=1,
                tokens=128,
            ),
        )


class SleepingExecutor:
    async def run(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        request: QueryText,
    ) -> RunResult:
        del tenant_id, session_id, run_id, request
        await asyncio.sleep(60)
        raise AssertionError("sleeping executor should be cancelled after lease loss")


class FakeDocumentStore(DocumentStoreTransaction):
    def __init__(self) -> None:
        self._collections: dict[str, dict[str, dict[str, object]]] = {}
        self._lock = asyncio.Lock()
        self.transaction_write_counts: list[int] = []

    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None:
        row = self._collections.get(collection, {}).get(document_id)
        if row is None:
            return None
        loaded = copy.deepcopy(row)
        loaded["document_id"] = document_id
        return loaded

    async def set(
        self, *, collection: str, document_id: str, document: Mapping[str, object]
    ) -> None:
        self._collections.setdefault(collection, {})[document_id] = copy.deepcopy(
            dict(document)
        )

    async def delete(self, *, collection: str, document_id: str) -> bool:
        rows = self._collections.get(collection, {})
        existed = document_id in rows
        rows.pop(document_id, None)
        return existed

    async def delete_known(self, *, collection: str, document_id: str) -> None:
        await self.delete(collection=collection, document_id=document_id)

    async def list(
        self,
        *,
        collection: str,
        document_id_prefix: str | None = None,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        start_after: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        selected = []
        for document_id, row in self._collections.get(collection, {}).items():
            if document_id_prefix is not None and not document_id.startswith(
                document_id_prefix
            ):
                continue
            if filters is not None and any(
                row.get(key) != value for key, value in filters.items()
            ):
                continue
            loaded = copy.deepcopy(row)
            loaded["document_id"] = document_id
            selected.append(loaded)
        if order_by:
            selected.sort(key=lambda row: tuple(row[field] for field in order_by))
        if start_after is not None:
            cursor = tuple(start_after[field] for field in order_by)
            selected = [
                row
                for row in selected
                if tuple(row[field] for field in order_by) > cursor
            ]
        if limit is not None:
            selected = selected[:limit]
        return tuple(selected)

    async def transaction[T](
        self, operation: Callable[[DocumentStoreTransaction], Awaitable[T]]
    ) -> T:
        async with self._lock:
            transaction = ReadBeforeWriteTransaction(self)
            result = await operation(transaction)
            self.transaction_write_counts.append(transaction.write_count)
            return result


class ReadBeforeWriteTransaction(DocumentStoreTransaction):
    def __init__(self, store: FakeDocumentStore) -> None:
        self._store = store
        self._wrote = False
        self.write_count = 0

    def _require_read_phase(self) -> None:
        if self._wrote:
            raise RuntimeError("Firestore transactions forbid read-after-write")

    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None:
        self._require_read_phase()
        return await self._store.get(collection=collection, document_id=document_id)

    async def list(
        self,
        *,
        collection: str,
        document_id_prefix: str | None = None,
        filters: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        start_after: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        self._require_read_phase()
        return await self._store.list(
            collection=collection,
            document_id_prefix=document_id_prefix,
            filters=filters,
            order_by=order_by,
            start_after=start_after,
            limit=limit,
        )

    async def set(
        self, *, collection: str, document_id: str, document: Mapping[str, object]
    ) -> None:
        self._wrote = True
        self.write_count += 1
        await self._store.set(
            collection=collection,
            document_id=document_id,
            document=document,
        )

    async def delete(self, *, collection: str, document_id: str) -> bool:
        self._wrote = True
        self.write_count += 1
        return await self._store.delete(
            collection=collection,
            document_id=document_id,
        )

    async def delete_known(self, *, collection: str, document_id: str) -> None:
        self._wrote = True
        await self._store.delete(collection=collection, document_id=document_id)


class OptimisticDocumentStore(FakeDocumentStore):
    """Let concurrent snapshots race, then retry exact-document conflicts."""

    async def transaction[T](
        self, operation: Callable[[DocumentStoreTransaction], Awaitable[T]]
    ) -> T:
        for _ in range(10):
            async with self._lock:
                original = copy.deepcopy(self._collections)
            snapshot = FakeDocumentStore()
            snapshot._collections = copy.deepcopy(original)
            tx = TrackingTransaction(snapshot)
            await asyncio.sleep(0)
            result = await operation(tx)
            async with self._lock:
                if any(
                    self._collections.get(collection, {}).get(document_id)
                    != original.get(collection, {}).get(document_id)
                    for collection, document_id in tx.reads
                ):
                    continue
                self._apply_changes(original, snapshot._collections)
                return result
        raise RuntimeError("transaction retries exhausted")

    def _apply_changes(
        self,
        original: Mapping[str, Mapping[str, dict[str, object]]],
        updated: Mapping[str, Mapping[str, dict[str, object]]],
    ) -> None:
        for collection in original.keys() | updated.keys():
            before = original.get(collection, {})
            after = updated.get(collection, {})
            for document_id in before.keys() | after.keys():
                if before.get(document_id) == after.get(document_id):
                    continue
                if document_id in after:
                    self._collections.setdefault(collection, {})[document_id] = (
                        copy.deepcopy(after[document_id])
                    )
                else:
                    self._collections.get(collection, {}).pop(document_id, None)


class TrackingTransaction(ReadBeforeWriteTransaction):
    def __init__(self, store: FakeDocumentStore) -> None:
        super().__init__(store)
        self.reads: set[tuple[str, str]] = set()

    async def get(
        self, *, collection: str, document_id: str
    ) -> dict[str, object] | None:
        self.reads.add((collection, document_id))
        return await super().get(collection=collection, document_id=document_id)


class FakeCloudTaskClient:
    def __init__(self) -> None:
        self._tasks: dict[str, CloudTask] = {}

    async def create(self, task: CloudTask) -> CloudTask:
        if task.name in self._tasks:
            raise CloudTaskAlreadyExistsError("task already exists")
        self._tasks[task.name] = task
        return task

    async def get(self, *, name: str) -> CloudTask | None:
        return self._tasks.get(name)

    async def delete(self, *, name: str) -> bool:
        return self._tasks.pop(name, None) is not None


@pytest.mark.asyncio
async def test_firestore_key_lifecycle_is_visible_across_replicas() -> None:
    store = FakeDocumentStore()
    manager_one = ApiKeyManager(FirestoreApiKeyRepository(store), FixedPepper())
    manager_two = ApiKeyManager(FirestoreApiKeyRepository(store), FixedPepper())
    generated = await manager_one.create(
        tenant_id="tenant-one",
        scopes=("runs:read",),
        now=NOW,
    )

    authenticated = await manager_two.authenticate(
        authorization=f"Bearer {generated.plaintext}",
        required_scope="runs:read",
        now=NOW,
    )
    assert authenticated.key_id == generated.record.key_id

    rotated = await manager_two.rotate(
        old_authorization=f"Bearer {generated.plaintext}",
        scopes=None,
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ApiKeyAuthError):
        await manager_one.authenticate(
            authorization=f"Bearer {generated.plaintext}",
            required_scope="runs:read",
            now=NOW + timedelta(seconds=1),
        )
    assert (
        await manager_one.authenticate(
            authorization=f"Bearer {rotated.plaintext}",
            required_scope="runs:read",
            now=NOW + timedelta(seconds=1),
        )
    ).key_id == rotated.record.key_id

    assert await manager_one.revoke(
        authorization=f"Bearer {rotated.plaintext}",
        now=NOW + timedelta(seconds=2),
    )
    with pytest.raises(ApiKeyAuthError):
        await manager_two.authenticate(
            authorization=f"Bearer {rotated.plaintext}",
            required_scope="runs:read",
            now=NOW + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_firestore_key_authentication_rejects_corrupted_scope() -> None:
    store = FakeDocumentStore()
    manager = ApiKeyManager(FirestoreApiKeyRepository(store), FixedPepper())
    generated = await manager.create(
        tenant_id="tenant-one",
        scopes=("runs:read",),
        now=NOW,
    )
    document_id = f"tenant-one|{generated.record.key_id}"
    document = await store.get(collection="api_key_hashes", document_id=document_id)
    assert document is not None
    await store.set(
        collection="api_key_hashes",
        document_id=document_id,
        document={**document, "tenant_id": "tenant-two"},
    )

    with pytest.raises(StorageError, match="scope is inconsistent"):
        await manager.authenticate(
            authorization=f"Bearer {generated.plaintext}",
            required_scope="runs:read",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_firestore_audit_is_idempotent_ordered_and_tenant_scoped() -> None:
    store = FakeDocumentStore()
    writer = FirestoreAuditRepository(store)
    reader = FirestoreAuditRepository(store)
    entries = (
        AuditEntry(
            tenant_id="tenant-one",
            entry_id="audit-two",
            action="run.completed",
            occurred_at=NOW + timedelta(seconds=1),
        ),
        AuditEntry(
            tenant_id="tenant-one",
            entry_id="audit-one",
            action="run.submitted",
            occurred_at=NOW,
        ),
        AuditEntry(
            tenant_id="tenant-two",
            entry_id="audit-one",
            action="run.submitted",
            occurred_at=NOW,
        ),
    )
    for entry in entries:
        assert await writer.append(entry)
    assert not await reader.append(entries[0])

    assert await reader.list(tenant_id="tenant-one") == (entries[1], entries[0])
    assert await reader.list(tenant_id="tenant-two") == (entries[2],)
    with pytest.raises(StorageConflictError):
        await reader.append(entries[0].model_copy(update={"action": "run.failed"}))


@pytest.mark.asyncio
async def test_firestore_quota_is_atomic_across_replicas_and_boundaries() -> None:
    store = OptimisticDocumentStore()
    config = LimitConfig(
        request_burst=1,
        requests_per_second=1,
        max_queued_runs=1,
        max_concurrent_runs=1,
        max_sse_connections=1,
        daily_work_units=1,
    )
    limiter_one = FirestoreQuotaLimiter(store, config)
    limiter_two = FirestoreQuotaLimiter(store, config)
    key_manager = ApiKeyManager(FirestoreApiKeyRepository(store), FixedPepper())
    quota_key = await key_manager.create(
        tenant_id="tenant-one",
        scopes=("runs:read",),
        now=NOW,
    )

    async def request(limiter: FirestoreQuotaLimiter) -> bool:
        try:
            await limiter.admit_request(
                tenant_id="tenant-one", key_id=quota_key.record.key_id, at=NOW
            )
        except QuotaExceeded:
            return False
        return True

    assert sorted(await asyncio.gather(request(limiter_one), request(limiter_two))) == [
        False,
        True,
    ]

    async def run(
        limiter: FirestoreQuotaLimiter, *, suffix: str
    ) -> RunAdmission | None:
        try:
            return await limiter.admit_run(
                tenant_id="tenant-one",
                key_id=quota_key.record.key_id,
                session_id="session-one",
                idempotency_key=f"request-{suffix}",
                query="Find the documented answer.",
                run_id=f"run-{suffix}",
                at=NOW,
            )
        except QuotaExceeded:
            return None

    admissions = await asyncio.gather(
        run(limiter_one, suffix="one"),
        run(limiter_two, suffix="two"),
    )
    assert sum(admission is not None for admission in admissions) == 1
    accepted = next(admission for admission in admissions if admission is not None)
    retried = await limiter_two.admit_run(
        tenant_id="tenant-one",
        key_id=quota_key.record.key_id,
        session_id="session-one",
        idempotency_key=accepted.idempotency_key,
        query="Find the documented answer.",
        run_id="run-discarded",
        at=NOW,
    )
    assert not retried.created and retried.run_id == accepted.run_id

    for run_id in ("run-one", "run-two"):
        await store.set(
            collection="runs",
            document_id=f"tenant-one|{run_id}",
            document={"tenant_id": "tenant-one", "run_id": run_id},
        )

    executions = await asyncio.gather(
        limiter_one.acquire_execution(
            tenant_id="tenant-one", run_id="run-one", at=NOW, lease_seconds=5
        ),
        limiter_two.acquire_execution(
            tenant_id="tenant-one", run_id="run-two", at=NOW, lease_seconds=5
        ),
    )
    assert sum(permit is not None for permit in executions) == 1
    blocked_run = "run-one" if executions[0] is None else "run-two"
    assert (
        await limiter_two.acquire_execution(
            tenant_id="tenant-one",
            run_id=blocked_run,
            at=NOW + timedelta(seconds=5),
            lease_seconds=5,
        )
        is not None
    )

    key = await key_manager.create(
        tenant_id="tenant-stream",
        scopes=("runs:read",),
        now=NOW,
    )
    stream = await limiter_one.acquire_sse(
        tenant_id="tenant-stream", key_id=key.record.key_id, at=NOW
    )
    with pytest.raises(QuotaExceeded):
        await limiter_two.acquire_sse(
            tenant_id="tenant-stream", key_id=key.record.key_id, at=NOW
        )
    assert await key_manager.revoke(
        authorization=f"Bearer {key.plaintext}", now=NOW + timedelta(seconds=1)
    )
    assert not await limiter_two.renew_sse(stream, at=NOW + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_firestore_execution_quota_reclaims_expired_leases() -> None:
    store = FakeDocumentStore()
    limiter = FirestoreQuotaLimiter(store, LimitConfig(max_concurrent_runs=1))
    for generation in range(3):
        run_id = f"run-{generation}-item"
        await store.set(
            collection="runs",
            document_id=f"tenant-one|{run_id}",
            document={"tenant_id": "tenant-one", "run_id": run_id},
        )
        assert await limiter.acquire_execution(
            tenant_id="tenant-one",
            run_id=run_id,
            at=NOW + timedelta(seconds=generation * 2),
            lease_seconds=1,
        )
        leases = await store.list(
            collection="quota_execution_leases",
            filters={"tenant_id": "tenant-one"},
        )
        assert len(leases) == 1


@pytest.mark.asyncio
async def test_firestore_lease_cleanup_uses_physical_document_identity() -> None:
    store = FakeDocumentStore()
    limiter = FirestoreQuotaLimiter(store, LimitConfig(max_concurrent_runs=2))
    await store.set(
        collection="runs",
        document_id="tenant-one|run-new-item",
        document={"tenant_id": "tenant-one", "run_id": "run-new-item"},
    )
    await store.set(
        collection="quota_execution_leases",
        document_id="tenant-one|run-expired-item",
        document={
            "document_id": "tenant-two|run-active-item",
            "tenant_id": "tenant-one",
            "run_id": "run-expired-item",
            "permit_id": "permit-expired",
            "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
        },
    )
    await store.set(
        collection="quota_execution_leases",
        document_id="tenant-two|run-active-item",
        document={
            "document_id": "tenant-two|run-active-item",
            "tenant_id": "tenant-two",
            "run_id": "run-active-item",
            "permit_id": "permit-active",
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        },
    )

    acquired = await limiter.acquire_execution(
        tenant_id="tenant-one",
        run_id="run-new-item",
        at=NOW,
        lease_seconds=30,
    )

    assert acquired is not None
    assert (
        await store.get(
            collection="quota_execution_leases",
            document_id="tenant-one|run-expired-item",
        )
        is None
    )
    assert (
        await store.get(
            collection="quota_execution_leases",
            document_id="tenant-two|run-active-item",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_firestore_lease_scan_rejects_missing_tenant_identity() -> None:
    store = FakeDocumentStore()
    limiter = FirestoreQuotaLimiter(store, LimitConfig(max_concurrent_runs=1))
    await store.set(
        collection="runs",
        document_id="tenant-one|run-new-item",
        document={"tenant_id": "tenant-one", "run_id": "run-new-item"},
    )
    await store.set(
        collection="quota_execution_leases",
        document_id="tenant-one|run-active-item",
        document={
            "document_id": "tenant-one|run-active-item",
            "run_id": "run-active-item",
            "permit_id": "permit-active",
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        },
    )

    with pytest.raises(StorageError, match="tenant_id"):
        await limiter.acquire_execution(
            tenant_id="tenant-one",
            run_id="run-new-item",
            at=NOW,
            lease_seconds=30,
        )


@pytest.mark.asyncio
async def test_firestore_sse_quota_reclaims_expired_leases() -> None:
    store = FakeDocumentStore()
    limiter = FirestoreQuotaLimiter(
        store,
        LimitConfig(max_sse_connections=1, sse_lease_seconds=30),
    )
    key = await ApiKeyManager(FirestoreApiKeyRepository(store), FixedPepper()).create(
        tenant_id="tenant-one", scopes=("runs:read",), now=NOW
    )

    for generation in range(3):
        await limiter.acquire_sse(
            tenant_id="tenant-one",
            key_id=key.record.key_id,
            at=NOW + timedelta(seconds=generation * 31),
        )
        leases = await store.list(
            collection="quota_sse_leases",
            filters={"tenant_id": "tenant-one"},
        )
        assert len(leases) == 1


@pytest.mark.asyncio
async def test_firestore_sse_lease_scan_rejects_missing_tenant_identity() -> None:
    store = FakeDocumentStore()
    limiter = FirestoreQuotaLimiter(store, LimitConfig(max_sse_connections=1))
    key = await ApiKeyManager(FirestoreApiKeyRepository(store), FixedPepper()).create(
        tenant_id="tenant-one", scopes=("runs:read",), now=NOW
    )
    await store.set(
        collection="quota_sse_leases",
        document_id="tenant-one|sse-active",
        document={
            "key_id": key.record.key_id,
            "permit_id": "sse-active",
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        },
    )

    with pytest.raises(StorageError, match="tenant_id"):
        await limiter.acquire_sse(
            tenant_id="tenant-one",
            key_id=key.record.key_id,
            at=NOW,
        )


@pytest.mark.asyncio
async def test_firestore_run_quota_refunds_failed_work_and_rolls_over_daily() -> None:
    store = FakeDocumentStore()
    limiter = FirestoreQuotaLimiter(
        store,
        LimitConfig(
            max_queued_runs=10,
            daily_work_units=1,
            pending_admission_seconds=5,
        ),
    )
    key = await ApiKeyManager(FirestoreApiKeyRepository(store), FixedPepper()).create(
        tenant_id="tenant-one", scopes=("runs:write",), now=NOW
    )
    common = {
        "tenant_id": "tenant-one",
        "key_id": key.record.key_id,
        "session_id": "session-one",
        "query": "Find the documented answer.",
    }
    first = await limiter.admit_run(
        **common,
        idempotency_key="request-one",
        run_id="run-one",
        at=NOW,
    )
    with pytest.raises(QuotaExceeded):
        await limiter.admit_run(
            **common,
            idempotency_key="request-two",
            run_id="run-two",
            at=NOW,
        )

    await limiter.release_run(first)
    replacement = await limiter.admit_run(
        **common,
        idempotency_key="request-two",
        run_id="run-two",
        at=NOW,
    )
    next_day = await limiter.admit_run(
        **common,
        idempotency_key="request-three",
        run_id="run-three",
        at=NOW + timedelta(days=1),
    )

    assert replacement.created and next_day.created


async def _seed_session(
    store: FakeDocumentStore, *, tenant_id: str, session_id: str
) -> None:
    await store.transaction(
        lambda tx: tx.set(
            collection="sessions",
            document_id=f"{tenant_id}|{session_id}",
            document={
                "document_id": f"{tenant_id}|{session_id}",
                "tenant_id": tenant_id,
                "session_id": session_id,
                "created_at": NOW.isoformat(timespec="microseconds"),
                "updated_at": NOW.isoformat(timespec="microseconds"),
            },
        )
    )


class TestFirestoreRunRepository(RunRepositoryContract):
    @pytest_asyncio.fixture
    async def repository(self, tmp_path: Path) -> AsyncIterator[RunRepository]:
        del tmp_path
        store = FakeDocumentStore()
        for tenant_id, session_id in (
            ("tenant-one", "session-one"),
            ("tenant-one", "session-two"),
            ("tenant-two", "session-one"),
        ):
            await _seed_session(store, tenant_id=tenant_id, session_id=session_id)
        yield FirestoreRunRepository(store)


class TestCloudTasksWorkQueue(WorkQueueContract):
    @pytest_asyncio.fixture
    async def queue(self, tmp_path: Path) -> AsyncIterator[WorkQueue]:
        del tmp_path
        store = FakeDocumentStore()
        for tenant_id in ("tenant-one", "tenant-two"):
            await _seed_session(store, tenant_id=tenant_id, session_id="session-one")
        runs = FirestoreRunRepository(store)
        await runs.create(submission())
        await runs.create(
            submission(
                tenant_id="tenant-two",
                run_id="run-one",
                idempotency_key="request-key-two",
            )
        )
        yield CloudTasksWorkQueue(
            store=store,
            task_client=FakeCloudTaskClient(),
            queue_name="projects/test/locations/eu/queues/dispatch",
            codec=SignedWorkItemCodec(b"s" * 32),
        )


@pytest.mark.asyncio
async def test_firestore_sessions_are_pageable_run_parents_and_cascade_delete() -> None:
    store = FakeDocumentStore()
    runs = FirestoreRunRepository(store)
    sessions = FirestoreSessionRepository(store, runs)
    first = SessionRecord(
        tenant_id="tenant-one",
        session_id="session-one",
        created_at=NOW,
        updated_at=NOW,
    )
    second = SessionRecord(
        tenant_id="tenant-one",
        session_id="session-two",
        created_at=NOW + timedelta(seconds=1),
        updated_at=NOW + timedelta(seconds=1),
    )

    assert await sessions.put(first) is True
    assert await sessions.put(first) is False
    assert await sessions.put(second) is True
    assert await sessions.list(tenant_id="tenant-one", limit=1) == (first,)
    assert await sessions.list(
        tenant_id="tenant-one",
        limit=1,
        after=(first.created_at, first.session_id),
    ) == (second,)

    created = await runs.create(submission())
    assert created.created is True
    assert await sessions.delete(tenant_id="tenant-one", session_id="session-one")
    assert await runs.get(tenant_id="tenant-one", run_id="run-one") is None
    assert await sessions.get(tenant_id="tenant-one", session_id="session-one") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reflection_count", [0, 1, 500, 501])
async def test_firestore_memory_delete_uses_size_safe_batches(
    reflection_count: int,
) -> None:
    store = FakeDocumentStore()
    sessions = FirestoreSessionRepository(store)
    for index in range(reflection_count):
        document_id = f"tenant-one|session-one|run-{index}"
        await store.set(
            collection="run_reflections",
            document_id=document_id,
            document={
                "document_id": document_id,
                "tenant_id": "tenant-one",
                "session_id": "session-one",
            },
        )
    await store.set(
        collection="run_reflections",
        document_id="tenant-two|session-one|run-other",
        document={
            "document_id": "tenant-two|session-one|run-other",
            "tenant_id": "tenant-two",
            "session_id": "session-one",
        },
    )

    assert (
        await sessions.delete_memory(tenant_id="tenant-one", session_id="session-one")
        == reflection_count
    )
    assert max(store.transaction_write_counts, default=0) <= 5
    assert (
        await store.get(
            collection="run_reflections",
            document_id="tenant-two|session-one|run-other",
        )
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cascade", ["session", "tenant"])
async def test_firestore_large_cascades_use_bounded_transactions(
    cascade: str,
) -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    for index in range(12):
        await runs.create(
            submission(
                run_id=f"run-{index:02d}",
                idempotency_key=f"request-key-{index:02d}",
            )
        )
    store.transaction_write_counts.clear()

    deleted = (
        await runs.delete_session(tenant_id="tenant-one", session_id="session-one")
        if cascade == "session"
        else await runs.delete_tenant(tenant_id="tenant-one")
    )

    assert deleted == 12
    assert max(store.transaction_write_counts) <= 5
    for collection in (
        "runs",
        "sessions",
        "idempotency_records",
        "run_events",
    ):
        assert (
            await store.list(collection=collection, filters={"tenant_id": "tenant-one"})
            == ()
        )


@pytest.mark.asyncio
async def test_firestore_run_delete_batches_large_child_collections() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    await runs.create(submission())
    for index in range(12):
        document_id = f"child-{index:02d}"
        document = {
            "document_id": document_id,
            "tenant_id": "tenant-one",
            "run_id": "run-one",
        }
        await store.set(
            collection="run_events",
            document_id=document_id,
            document=document,
        )
        await store.set(
            collection="work_items",
            document_id=document_id,
            document=document,
        )
    store.transaction_write_counts.clear()

    assert await runs.delete_run(tenant_id="tenant-one", run_id="run-one")

    assert max(store.transaction_write_counts) <= 5
    for collection in (
        "runs",
        "idempotency_records",
        "run_events",
        "work_items",
    ):
        assert (
            await store.list(collection=collection, filters={"tenant_id": "tenant-one"})
            == ()
        )


@pytest.mark.asyncio
async def test_firestore_event_ordering_and_finality_match_local_contract() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    events = FirestoreEventRepository(store)
    await runs.create(submission())

    for sequence in (2, 3, 4):
        assert await events.append(
            tenant_id="tenant-one",
            event=RunEvent(
                sequence=sequence,
                run_id="run-one",
                event_type=RunEventType.STATUS,
                state=RunState.QUEUED,
                occurred_at=NOW + timedelta(seconds=sequence),
                message="Run is queued.",
            ),
        )

    cancelled = await runs.request_cancellation(
        tenant_id="tenant-one",
        run_id="run-one",
        at=NOW + timedelta(seconds=5),
    )

    assert cancelled.changed is True
    resumed = await events.list(
        tenant_id="tenant-one",
        run_id="run-one",
        after_sequence=4,
    )
    assert len(resumed) == 1
    assert resumed[0].sequence == 5
    assert resumed[0].event_type is RunEventType.CANCELLED
    with pytest.raises(StorageConflictError, match="terminal event must remain final"):
        await events.append(
            tenant_id="tenant-one",
            event=RunEvent(
                sequence=6,
                run_id="run-one",
                event_type=RunEventType.STATUS,
                state=RunState.RUNNING,
                occurred_at=NOW + timedelta(seconds=6),
                message="Run execution is in progress.",
            ),
        )


@pytest.mark.asyncio
async def test_cloud_task_payloads_are_signed_and_tamper_evident() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    await runs.create(submission())
    client = FakeCloudTaskClient()
    queue_name = "projects/test/locations/eu/queues/dispatch"
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=client,
        queue_name=queue_name,
        codec=SignedWorkItemCodec(b"s" * 32),
    )
    item = WorkItem(
        work_id="work-one",
        tenant_id="tenant-one",
        run_id="run-one",
        enqueued_at=NOW,
        not_before=NOW,
    )

    created = await queue.enqueue(item)
    task = next(iter(client._tasks.values()))
    delivery_headers = dict(task.headers) | {
        "X-CloudTasks-TaskName": task.name,
        "X-CloudTasks-QueueName": queue_name,
    }

    assert created.created is True
    assert task is not None
    assert (
        queue.decode_delivery(
            body=task.body,
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=delivery_headers["X-CloudTasks-TaskName"],
            queue_name=delivery_headers["X-CloudTasks-QueueName"],
        )
        == created.item
    )
    assert (
        queue.decode_delivery(
            body=task.body,
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=task.name.rsplit("/", 1)[-1],
            queue_name=queue_name.rsplit("/", 1)[-1],
        )
        == created.item
    )
    with pytest.raises(TaskDeliveryAuthError, match="signature is invalid"):
        queue.decode_delivery(
            body=task.body + b" ",
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=delivery_headers["X-CloudTasks-TaskName"],
            queue_name=delivery_headers["X-CloudTasks-QueueName"],
        )
    other_queue = "projects/other/locations/us/queues/dispatch"
    with pytest.raises(TaskDeliveryAuthError):
        queue.decode_delivery(
            body=task.body,
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=f"{other_queue}/tasks/{task.name.rsplit('/', 1)[-1]}",
            queue_name=other_queue,
        )
    with pytest.raises(TaskDeliveryAuthError, match="headers are incomplete"):
        queue.decode_delivery(
            body=task.body,
            signature=None,
            task_name=delivery_headers["X-CloudTasks-TaskName"],
            queue_name=delivery_headers["X-CloudTasks-QueueName"],
        )
    with pytest.raises(TaskDeliveryAuthError, match="queue is invalid"):
        queue.decode_delivery(
            body=task.body,
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=delivery_headers["X-CloudTasks-TaskName"],
            queue_name="projects/test/locations/eu/queues/other",
        )
    with pytest.raises(TaskDeliveryAuthError, match="signature is invalid"):
        queue.decode_delivery(
            body=task.body,
            signature=delivery_headers["X-Agent-Api-Task-Signature"],
            task_name=f"{queue_name}/tasks/other",
            queue_name=delivery_headers["X-CloudTasks-QueueName"],
        )


@pytest.mark.asyncio
async def test_signed_cloud_delivery_executes_once_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    repository = FirestoreRunRepository(store)
    events = FirestoreEventRepository(store)
    client = FakeCloudTaskClient()
    queue_name = "projects/test/locations/eu/queues/dispatch"
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=client,
        queue_name=queue_name,
        codec=SignedWorkItemCodec(b"s" * 32),
    )
    executor = CompletedExecutor()
    app = create_app(
        database_path=tmp_path / "cloud-delivery.sqlite3",
        pepper_provider=FixedPepper(),
        run_executor=executor,
        run_repository=repository,
        event_repository=events,
        work_queue=queue,
        production_environment=True,
        run_state_backend="firestore",
        queue_backend="cloud_tasks",
        task_delivery_enabled=True,
    )
    created = await repository.create(submission())
    item = WorkItem(
        work_id=f"work-{created.run.run_id}",
        tenant_id=created.run.tenant_id,
        run_id=created.run.run_id,
        enqueued_at=created.run.created_at,
        not_before=created.run.created_at,
    )
    await queue.enqueue(item)
    task = next(iter(client._tasks.values()))
    headers = {name: value for name, value in task.headers} | {
        "X-CloudTasks-TaskName": task.name.rsplit("/", 1)[-1],
        "X-CloudTasks-QueueName": queue_name.rsplit("/", 1)[-1],
    }

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as web,
    ):
        first = await web.post(
            "/internal/tasks/run-delivery",
            content=task.body,
            headers=headers,
        )
        duplicate = await web.post(
            "/internal/tasks/run-delivery", content=task.body, headers=headers
        )
        tampered = await web.post(
            "/internal/tasks/run-delivery",
            content=task.body + b" ",
            headers=headers,
        )

    assert first.status_code == 204
    assert duplicate.status_code == 204
    assert tampered.status_code == 401
    assert tampered.json()["error"]["code"] == "unauthenticated"
    assert executor.calls == [
        (
            "tenant-one",
            "session-one",
            created.run.run_id,
            "find the documented answer",
        )
    ]
    stored = await repository.get(tenant_id="tenant-one", run_id=created.run.run_id)
    assert stored is not None
    assert stored.state is RunState.COMPLETED
    assert stored.delivery_attempts == 1
    assert tuple(
        event.state
        for event in await events.list(
            tenant_id="tenant-one", run_id=created.run.run_id
        )
    ) == (
        RunState.QUEUED,
        RunState.RUNNING,
        RunState.COMPLETED,
    )


@pytest.mark.parametrize("contention", ["quota", "lease"])
@pytest.mark.asyncio
async def test_temporary_cloud_delivery_contention_returns_retryable_status(
    tmp_path: Path,
    contention: str,
) -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    await runs.create(submission())
    client = FakeCloudTaskClient()
    queue_name = "projects/test/locations/eu/queues/dispatch"
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=client,
        queue_name=queue_name,
        codec=SignedWorkItemCodec(b"s" * 32),
    )
    await queue.enqueue(
        WorkItem(
            work_id="work-run-one",
            tenant_id="tenant-one",
            run_id="run-one",
            enqueued_at=NOW,
            not_before=NOW,
        )
    )
    if contention == "lease":
        await runs.claim(
            ClaimRequest(
                tenant_id="tenant-one",
                run_id="run-one",
                worker_id="worker-crashed",
                lease_id="lease-crashed",
                now=NOW,
                lease_seconds=30,
            )
        )
    task = next(iter(client._tasks.values()))
    app = create_app(
        database_path=tmp_path / f"{contention}-delivery.sqlite3",
        pepper_provider=FixedPepper(),
        clock=lambda: NOW + timedelta(seconds=1),
        quota_limiter=FirestoreQuotaLimiter(
            store,
            LimitConfig(max_concurrent_runs=0 if contention == "quota" else 4),
        ),
        run_executor=CompletedExecutor(),
        run_repository=runs,
        event_repository=FirestoreEventRepository(store),
        work_queue=queue,
        production_environment=True,
        run_state_backend="firestore",
        queue_backend="cloud_tasks",
        task_delivery_enabled=True,
    )
    headers = dict(task.headers) | {
        "X-CloudTasks-TaskName": task.name.rsplit("/", 1)[-1],
        "X-CloudTasks-QueueName": queue_name.rsplit("/", 1)[-1],
    }

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as web,
    ):
        response = await web.post(
            "/internal/tasks/run-delivery",
            content=task.body,
            headers=headers,
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"


@pytest.mark.asyncio
async def test_production_application_creates_cloud_run_parent_sessions(
    tmp_path: Path,
) -> None:
    store = FakeDocumentStore()
    runs = FirestoreRunRepository(store)
    database_path = tmp_path / "cloud-session.sqlite3"
    app = create_app(
        database_path=database_path,
        pepper_provider=FixedPepper(),
        session_id_factory=lambda: "session-cloud",
        run_repository=runs,
        event_repository=FirestoreEventRepository(store),
        work_queue=CloudTasksWorkQueue(
            store=store,
            task_client=FakeCloudTaskClient(),
            queue_name="projects/test/locations/eu/queues/dispatch",
            codec=SignedWorkItemCodec(b"s" * 32),
        ),
        production_environment=True,
        run_state_backend="firestore",
        queue_backend="cloud_tasks",
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as web,
    ):
        session = await app.state.session_service.create(
            tenant_id="tenant-one", label=None
        )
        created = await runs.create(
            submission(session_id=session.session_id, run_id="run-cloud")
        )
        readiness = await web.get("/health/ready")

    assert created.created is True
    assert readiness.status_code == 200
    assert not database_path.exists()


@pytest.mark.asyncio
async def test_production_replicas_share_security_authority_without_sqlite(
    tmp_path: Path,
) -> None:
    store = FakeDocumentStore()
    runs = FirestoreRunRepository(store)
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=FakeCloudTaskClient(),
        queue_name="projects/test/locations/eu/queues/dispatch",
        codec=SignedWorkItemCodec(b"s" * 32),
    )

    def replica(name: str) -> tuple[Path, FastAPI]:
        path = tmp_path / f"{name}.sqlite3"
        app = create_app(
            database_path=path,
            pepper_provider=FixedPepper(),
            run_repository=runs,
            event_repository=FirestoreEventRepository(store),
            work_queue=queue,
            production_environment=True,
            run_state_backend="firestore",
            queue_backend="cloud_tasks",
            limit_config=LimitConfig(request_burst=1, requests_per_second=1),
        )
        return path, app

    path_one, app_one = replica("replica-one")
    path_two, app_two = replica("replica-two")
    async with (
        app_one.router.lifespan_context(app_one),
        app_two.router.lifespan_context(app_two),
    ):
        generated = await app_one.state.auth_manager.create(
            tenant_id="tenant-one",
            scopes=("runs:read",),
            now=NOW,
        )
        authenticated = await app_two.state.auth_manager.authenticate(
            authorization=f"Bearer {generated.plaintext}",
            required_scope="runs:read",
            now=NOW,
        )
        await app_one.state.quota_limiter.admit_request(
            tenant_id="tenant-one", key_id=authenticated.key_id, at=NOW
        )
        with pytest.raises(QuotaExceeded):
            await app_two.state.quota_limiter.admit_request(
                tenant_id="tenant-one", key_id=authenticated.key_id, at=NOW
            )
        await app_one.state.telemetry.run_submitted(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-one",
            correlation_id="request-one",
            at=NOW,
        )

    assert authenticated.key_id == generated.record.key_id
    audit = await FirestoreAuditRepository(store).list(tenant_id="tenant-one")
    assert len(audit) == 1 and audit[0].action == "run.submitted"
    assert isinstance(app_one.state.quota_limiter, FirestoreQuotaLimiter)
    assert isinstance(app_two.state.quota_limiter, FirestoreQuotaLimiter)
    assert not path_one.exists() and not path_two.exists()


def test_production_rejects_sqlite_adapters_with_cloud_backend_labels(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sqlite-disguised-as-cloud.sqlite3"
    path.touch()

    with pytest.raises(ValueError, match=r"Firestore|Cloud Tasks"):
        create_app(
            database_path=path,
            pepper_provider=FixedPepper(),
            run_repository=SQLiteRunRepository(path),
            event_repository=SQLiteEventRepository(path),
            work_queue=SQLiteWorkQueue(path),
            production_environment=True,
            run_state_backend="firestore",
            queue_backend="cloud_tasks",
        )


def test_production_rejects_cloud_adapters_with_split_document_stores(
    tmp_path: Path,
) -> None:
    session_store = FakeDocumentStore()
    run_store = FakeDocumentStore()

    with pytest.raises(ValueError, match="same document store"):
        create_app(
            database_path=tmp_path / "split-cloud.sqlite3",
            pepper_provider=FixedPepper(),
            session_repository=FirestoreSessionRepository(session_store),
            run_repository=FirestoreRunRepository(run_store),
            event_repository=FirestoreEventRepository(run_store),
            work_queue=CloudTasksWorkQueue(
                store=run_store,
                task_client=FakeCloudTaskClient(),
                queue_name="projects/test/locations/eu/queues/dispatch",
                codec=SignedWorkItemCodec(b"s" * 32),
            ),
            production_environment=True,
            run_state_backend="firestore",
            queue_backend="cloud_tasks",
        )


def test_production_rejects_local_quota_with_otherwise_shared_cloud_state(
    tmp_path: Path,
) -> None:
    store = FakeDocumentStore()
    runs = FirestoreRunRepository(store)
    database_path = tmp_path / "local-quota.sqlite3"
    database_path.touch()

    with pytest.raises(ValueError, match="Firestore quota state"):
        create_app(
            database_path=database_path,
            pepper_provider=FixedPepper(),
            quota_limiter=SQLiteQuotaLimiter(database_path, LimitConfig()),
            run_repository=runs,
            event_repository=FirestoreEventRepository(store),
            work_queue=CloudTasksWorkQueue(
                store=store,
                task_client=FakeCloudTaskClient(),
                queue_name="projects/test/locations/eu/queues/dispatch",
                codec=SignedWorkItemCodec(b"s" * 32),
            ),
            production_environment=True,
            run_state_backend="firestore",
            queue_backend="cloud_tasks",
        )


def test_production_rejects_custom_telemetry_that_can_hide_local_audit(
    tmp_path: Path,
) -> None:
    store = FakeDocumentStore()
    database_path = tmp_path / "local-audit.sqlite3"
    database_path.touch()

    with pytest.raises(ValueError, match="shared audit authority"):
        create_app(
            database_path=database_path,
            pepper_provider=FixedPepper(),
            telemetry=OperationalTelemetry(
                pseudonym_key=b"p" * 32,
                audit=SQLiteAuditRepository(database_path),
            ),
            run_repository=FirestoreRunRepository(store),
            event_repository=FirestoreEventRepository(store),
            work_queue=CloudTasksWorkQueue(
                store=store,
                task_client=FakeCloudTaskClient(),
                queue_name="projects/test/locations/eu/queues/dispatch",
                codec=SignedWorkItemCodec(b"s" * 32),
            ),
            production_environment=True,
            run_state_backend="firestore",
            queue_backend="cloud_tasks",
        )


def test_session_repository_rejects_split_cascade_store() -> None:
    with pytest.raises(ValueError, match="same document store"):
        FirestoreSessionRepository(
            FakeDocumentStore(),
            FirestoreRunRepository(FakeDocumentStore()),
        )


@pytest.mark.asyncio
async def test_mid_execution_lease_loss_requests_delivery_retry() -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    await runs.create(submission())
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=FakeCloudTaskClient(),
        queue_name="projects/test/locations/eu/queues/dispatch",
        codec=SignedWorkItemCodec(b"s" * 32),
    )
    item = WorkItem(
        work_id="work-run-one",
        tenant_id="tenant-one",
        run_id="run-one",
        enqueued_at=NOW,
        not_before=NOW,
    )
    enqueued = await queue.enqueue(item)
    times = iter((NOW, NOW + timedelta(seconds=31)))
    worker = LocalWorker(
        repository=runs,
        queue=queue,
        executor=SleepingExecutor(),
        worker_id="worker-one",
        clock=lambda: next(times),
        lease_seconds=30,
        heartbeat_seconds=0.01,
        cancellation_drain_seconds=0.1,
    )

    assert await worker.process(enqueued.item) is False


@pytest.mark.parametrize(
    ("event_repository", "run_executor", "task_delivery_enabled", "message"),
    [
        (None, None, False, "injected event repository"),
        ("injected", CompletedExecutor(), False, "signed task delivery"),
    ],
)
def test_production_runtime_rejects_partial_cloud_wiring(
    tmp_path: Path,
    event_repository: object,
    run_executor: CompletedExecutor | None,
    task_delivery_enabled: bool,
    message: str,
) -> None:
    store = FakeDocumentStore()
    runs = FirestoreRunRepository(store)
    events = FirestoreEventRepository(store)
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=FakeCloudTaskClient(),
        queue_name="projects/test/locations/eu/queues/dispatch",
        codec=SignedWorkItemCodec(b"s" * 32),
    )

    with pytest.raises(ValueError, match=message):
        create_app(
            database_path=tmp_path / "partial-cloud.sqlite3",
            pepper_provider=FixedPepper(),
            run_executor=run_executor,
            run_repository=runs,
            event_repository=None if event_repository is None else events,
            work_queue=queue,
            production_environment=True,
            run_state_backend="firestore",
            queue_backend="cloud_tasks",
            task_delivery_enabled=task_delivery_enabled,
        )


@pytest.mark.asyncio
async def test_firestore_concurrent_run_deletes_have_one_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    await runs.create(submission())
    original_get = runs.get
    both_read = asyncio.Event()
    reads = 0

    async def synchronized_get(*, tenant_id: OpaqueId, run_id: OpaqueId) -> object:
        nonlocal reads
        stored = await original_get(tenant_id=tenant_id, run_id=run_id)
        reads += 1
        if reads == 2:
            both_read.set()
        await both_read.wait()
        return stored

    monkeypatch.setattr(runs, "get", synchronized_get)

    results = await asyncio.gather(
        runs.delete_run(tenant_id="tenant-one", run_id="run-one"),
        runs.delete_run(tenant_id="tenant-one", run_id="run-one"),
    )

    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_cloud_enqueue_rechecks_parent_after_remote_creation() -> None:
    class PausedTaskClient(FakeCloudTaskClient):
        def __init__(self) -> None:
            super().__init__()
            self.create_started = asyncio.Event()
            self.resume_create = asyncio.Event()

        async def create(self, task: CloudTask) -> CloudTask:
            self.create_started.set()
            await self.resume_create.wait()
            return await super().create(task)

    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    await runs.create(submission())
    client = PausedTaskClient()
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=client,
        queue_name="projects/test/locations/eu/queues/dispatch",
        codec=SignedWorkItemCodec(b"s" * 32),
    )
    enqueue = asyncio.create_task(
        queue.enqueue(
            WorkItem(
                work_id="work-one",
                tenant_id="tenant-one",
                run_id="run-one",
                enqueued_at=NOW,
                not_before=NOW,
            )
        )
    )
    await client.create_started.wait()

    assert await runs.delete_run(tenant_id="tenant-one", run_id="run-one")
    client.resume_create.set()
    with pytest.raises(RunParentNotFoundError):
        await enqueue

    assert (
        await store.list(
            collection="work_items",
            filters={"tenant_id": "tenant-one", "run_id": "run-one"},
        )
        == ()
    )


@pytest.mark.asyncio
async def test_cloud_enqueue_rejects_a_recreated_parent_generation() -> None:
    class PausedTaskClient(FakeCloudTaskClient):
        def __init__(self) -> None:
            super().__init__()
            self.create_started = asyncio.Event()
            self.resume_create = asyncio.Event()

        async def create(self, task: CloudTask) -> CloudTask:
            created = await super().create(task)
            self.create_started.set()
            await self.resume_create.wait()
            return created

    store = FakeDocumentStore()
    await _seed_session(store, tenant_id="tenant-one", session_id="session-one")
    runs = FirestoreRunRepository(store)
    await runs.create(submission())
    client = PausedTaskClient()
    queue = CloudTasksWorkQueue(
        store=store,
        task_client=client,
        queue_name="projects/test/locations/eu/queues/dispatch",
        codec=SignedWorkItemCodec(b"s" * 32),
    )
    enqueue = asyncio.create_task(
        queue.enqueue(
            WorkItem(
                work_id="work-one",
                tenant_id="tenant-one",
                run_id="run-one",
                enqueued_at=NOW,
                not_before=NOW,
            )
        )
    )
    await client.create_started.wait()

    assert await runs.delete_run(tenant_id="tenant-one", run_id="run-one")
    replacement = submission().model_copy(
        update={
            "idempotency_key": "request-key-two",
            "query": "a different request for the reused run id",
            "created_at": NOW + timedelta(seconds=1),
        }
    )
    await runs.create(replacement)
    client.resume_create.set()

    with pytest.raises(RunParentNotFoundError):
        await enqueue
    assert not await store.list(
        collection="work_items",
        filters={"tenant_id": "tenant-one", "run_id": "run-one"},
    )
    assert not client._tasks
