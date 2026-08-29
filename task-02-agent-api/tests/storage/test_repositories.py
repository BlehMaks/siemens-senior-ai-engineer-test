from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import AnyHttpUrl

from agent_api.ports import (
    ClaimRequest,
    RunFailureCode,
    RunState,
    RunSubmission,
    StateUpdate,
    WorkItem,
)
from agent_api.schemas import RunEvent, RunEventType
from agent_api.storage import (
    ApiKeyHashRecord,
    AuditEntry,
    SessionRecord,
    SQLiteAuditRepository,
    SQLiteEventRepository,
    SQLiteKeyHashRepository,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    SQLiteWorkQueue,
    StorageConflictError,
    StorageError,
    TenantRecord,
    reflection_repository,
)
from search_agent.contracts import Citation, EventType, ScopedAnswer, TerminalState
from search_agent.memory import (
    CompletionEvidence,
    ReflectionStorageError,
    ReflectionUsage,
    RunReflection,
    UnresolvedItem,
)

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


def submission(
    *,
    tenant_id: str = "tenant-one",
    session_id: str = "session-one",
    run_id: str = "run-one",
    key: str = "request-key-one",
    created_at: datetime = NOW,
) -> RunSubmission:
    return RunSubmission(
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
        idempotency_key=key,
        query="find the documented answer",
        created_at=created_at,
    )


def reflection(*, tenant_id: str, run_id: str) -> RunReflection:
    return RunReflection(
        tenant_id=tenant_id,
        session_id="session-one",
        run_id=run_id,
        requested_outcome="Find the public Siemens report.",
        actions=(EventType.EVIDENCE_READY,),
        failures=(),
        recovery_steps=(),
        completion_evidence=(
            CompletionEvidence(
                evidence_id="ev-public",
                source_url=AnyHttpUrl("https://example.com/report"),
            ),
        ),
        unresolved_items=(),
        outcome=TerminalState.COMPLETED,
        usage=ReflectionUsage(
            elapsed_seconds=0,
            iterations=0,
            search_queries=0,
            pages=0,
            failed_pages=0,
            raw_bytes_reserved=0,
            decoded_bytes=0,
            model_calls=0,
            model_attempts=0,
            tokens=0,
        ),
    )


def failed_reflection(*, tenant_id: str, run_id: str) -> RunReflection:
    return RunReflection(
        tenant_id=tenant_id,
        session_id="session-one",
        run_id=run_id,
        requested_outcome="Find the public Siemens report.",
        actions=(),
        failures=(),
        recovery_steps=(),
        completion_evidence=(),
        unresolved_items=(UnresolvedItem.NO_EVIDENCE,),
        outcome=TerminalState.FAILED,
        usage=ReflectionUsage(
            elapsed_seconds=0,
            iterations=0,
            search_queries=0,
            pages=0,
            failed_pages=0,
            raw_bytes_reserved=0,
            decoded_bytes=0,
            model_calls=0,
            model_attempts=0,
            tokens=0,
        ),
    )


async def seed_session(
    path: Path, *, tenant_id: str = "tenant-one", session_id: str = "session-one"
) -> None:
    await SQLiteTenantRepository(path).put(
        TenantRecord(tenant_id=tenant_id, created_at=NOW)
    )
    await SQLiteSessionRepository(path).put(
        SessionRecord(
            tenant_id=tenant_id,
            session_id=session_id,
            created_at=NOW,
            updated_at=NOW,
        )
    )


@pytest.mark.asyncio
async def test_run_and_event_order_survive_reopen(migrated_path: Path) -> None:
    await seed_session(migrated_path)
    runs = SQLiteRunRepository(migrated_path)
    for run in (
        submission(run_id="run-zed", key="request-key-zed"),
        submission(run_id="run-alpha", key="request-key-alpha"),
        submission(
            run_id="run-later",
            key="request-key-later",
            created_at=NOW + timedelta(seconds=1),
        ),
    ):
        await runs.create(run)

    events = SQLiteEventRepository(migrated_path)
    for sequence in (2, 3, 4):
        await events.append(
            tenant_id="tenant-one",
            event=RunEvent(
                sequence=sequence,
                run_id="run-alpha",
                event_type=RunEventType.STATUS,
                state=RunState.QUEUED,
                occurred_at=NOW + timedelta(seconds=sequence),
                message="Run accepted.",
            ),
        )

    reopened_runs = SQLiteRunRepository(migrated_path)
    reopened_events = SQLiteEventRepository(migrated_path)
    assert tuple(
        run.run_id
        for run in await reopened_runs.list_session(
            tenant_id="tenant-one", session_id="session-one"
        )
    ) == ("run-alpha", "run-zed", "run-later")
    assert tuple(
        event.sequence
        for event in await reopened_events.list(
            tenant_id="tenant-one", run_id="run-alpha"
        )
    ) == (1, 2, 3, 4)
    assert await reopened_events.list(tenant_id="tenant-two", run_id="run-alpha") == ()

    cancelled = await reopened_runs.request_cancellation(
        tenant_id="tenant-one",
        run_id="run-alpha",
        at=NOW + timedelta(seconds=5),
    )
    assert cancelled.changed
    resumed = await reopened_events.list(
        tenant_id="tenant-one", run_id="run-alpha", after_sequence=4
    )
    assert len(resumed) == 1
    assert resumed[0].sequence == 5
    assert resumed[0].event_type is RunEventType.CANCELLED
    assert resumed[0].state is RunState.CANCELLED
    with pytest.raises(StorageConflictError, match="terminal event must remain final"):
        await reopened_events.append(
            tenant_id="tenant-one",
            event=RunEvent(
                sequence=6,
                run_id="run-alpha",
                event_type=RunEventType.STATUS,
                state=RunState.RUNNING,
                occurred_at=NOW + timedelta(seconds=6),
                message="Run execution is in progress.",
            ),
        )


@pytest.mark.asyncio
async def test_idempotent_create_and_cancel_emit_one_event_per_change(
    migrated_path: Path,
) -> None:
    await seed_session(migrated_path)
    runs = SQLiteRunRepository(migrated_path)

    first = await runs.create(submission())
    duplicate = await runs.create(submission())
    cancelled = await runs.request_cancellation(
        tenant_id="tenant-one", run_id="run-one", at=NOW
    )
    repeated = await runs.request_cancellation(
        tenant_id="tenant-one", run_id="run-one", at=NOW
    )

    assert first.created
    assert not duplicate.created
    assert cancelled.changed
    assert not repeated.changed
    events = await SQLiteEventRepository(migrated_path).list(
        tenant_id="tenant-one", run_id="run-one"
    )
    assert tuple(
        (event.sequence, event.event_type, event.state) for event in events
    ) == (
        (1, RunEventType.STATUS, RunState.QUEUED),
        (2, RunEventType.CANCELLED, RunState.CANCELLED),
    )


@pytest.mark.asyncio
async def test_event_append_rejects_a_new_out_of_order_sequence(
    migrated_path: Path,
) -> None:
    await seed_session(migrated_path)
    await SQLiteRunRepository(migrated_path).create(submission())
    events = SQLiteEventRepository(migrated_path)
    with pytest.raises(StorageConflictError, match="event does not match run state"):
        await events.append(
            tenant_id="tenant-one",
            event=RunEvent(
                sequence=2,
                run_id="run-one",
                event_type=RunEventType.CANCELLED,
                state=RunState.CANCELLED,
                occurred_at=NOW + timedelta(seconds=2),
                message="Run cancelled.",
            ),
        )
    assert await events.append(
        tenant_id="tenant-one",
        event=RunEvent(
            sequence=3,
            run_id="run-one",
            event_type=RunEventType.STATUS,
            state=RunState.QUEUED,
            occurred_at=NOW + timedelta(seconds=3),
            message="Run is queued.",
        ),
    )

    with pytest.raises(StorageConflictError, match="event sequence must increase"):
        await events.append(
            tenant_id="tenant-one",
            event=RunEvent(
                sequence=2,
                run_id="run-one",
                event_type=RunEventType.STATUS,
                state=RunState.QUEUED,
                occurred_at=NOW + timedelta(seconds=2),
                message="Run is queued.",
            ),
        )


@pytest.mark.asyncio
async def test_tenant_session_key_audit_and_memory_are_isolated(
    migrated_path: Path,
) -> None:
    tenants = SQLiteTenantRepository(migrated_path)
    sessions = SQLiteSessionRepository(migrated_path)
    keys = SQLiteKeyHashRepository(migrated_path)
    audit = SQLiteAuditRepository(migrated_path)
    for tenant_id in ("tenant-one", "tenant-two"):
        assert await tenants.put(TenantRecord(tenant_id=tenant_id, created_at=NOW))
        assert await sessions.put(
            SessionRecord(
                tenant_id=tenant_id,
                session_id="session-one",
                label=f"Session for {tenant_id}",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        assert await keys.put(
            ApiKeyHashRecord(
                tenant_id=tenant_id,
                key_id="key-one",
                key_hash=bytes(32) if tenant_id == "tenant-one" else bytes([1]) * 32,
                created_at=NOW,
            )
        )
        assert await audit.append(
            AuditEntry(
                tenant_id=tenant_id,
                entry_id="audit-one",
                action="session.created",
                occurred_at=NOW,
            )
        )

    memory = reflection_repository(migrated_path)
    try:
        memory.put(reflection(tenant_id="tenant-one", run_id="run-one"))
        memory.put(reflection(tenant_id="tenant-two", run_id="run-two"))
    finally:
        memory.close()

    assert await sessions.get(
        tenant_id="tenant-one", session_id="session-one"
    ) != await sessions.get(tenant_id="tenant-two", session_id="session-one")
    stored_key = await keys.get(tenant_id="tenant-one", key_id="key-one")
    assert stored_key is not None
    assert stored_key.key_hash == bytes(32)
    assert await keys.get(tenant_id="tenant-three", key_id="key-one") is None
    assert len(await audit.list(tenant_id="tenant-one")) == 1
    assert await audit.list(tenant_id="tenant-three") == ()

    assert await tenants.delete(tenant_id="tenant-one")
    assert await sessions.get(tenant_id="tenant-one", session_id="session-one") is None
    assert await sessions.get(tenant_id="tenant-two", session_id="session-one")
    reopened_memory = reflection_repository(migrated_path)
    try:
        assert (
            reopened_memory.list_session(
                tenant_id="tenant-one", session_id="session-one"
            )
            == ()
        )
        assert (
            len(
                reopened_memory.list_session(
                    tenant_id="tenant-two", session_id="session-one"
                )
            )
            == 1
        )
    finally:
        reopened_memory.close()


@pytest.mark.asyncio
async def test_plaintext_api_keys_have_no_schema_or_storage_path(
    migrated_path: Path,
) -> None:
    tenants = SQLiteTenantRepository(migrated_path)
    await tenants.put(TenantRecord(tenant_id="tenant-one", created_at=NOW))
    keys = SQLiteKeyHashRepository(migrated_path)
    private_key = "private-api-key-that-must-not-be-stored"
    await keys.put(
        ApiKeyHashRecord(
            tenant_id="tenant-one",
            key_id="key-one",
            key_hash=b"h" * 32,
            created_at=NOW,
        )
    )

    with sqlite3.connect(migrated_path) as connection:
        columns = tuple(
            row[1] for row in connection.execute("PRAGMA table_info(api_key_hashes)")
        )
    assert "plaintext_key" not in columns
    assert private_key.encode() not in migrated_path.read_bytes()


@pytest.mark.asyncio
async def test_corrupt_key_hash_fails_safe_without_echoing_storage(
    migrated_path: Path,
) -> None:
    await SQLiteTenantRepository(migrated_path).put(
        TenantRecord(tenant_id="tenant-one", created_at=NOW)
    )
    keys = SQLiteKeyHashRepository(migrated_path)
    await keys.put(
        ApiKeyHashRecord(
            tenant_id="tenant-one",
            key_id="key-one",
            key_hash=b"h" * 32,
            created_at=NOW,
        )
    )
    sentinel = "credential-private-sentinel".ljust(32, "x")
    with sqlite3.connect(migrated_path) as connection:
        connection.execute(
            "UPDATE api_key_hashes SET key_hash = ? WHERE tenant_id = ? AND key_id = ?",
            (sentinel, "tenant-one", "key-one"),
        )

    with pytest.raises(StorageError) as error:
        await keys.get(tenant_id="tenant-one", key_id="key-one")
    assert sentinel not in str(error.value)


@pytest.mark.asyncio
async def test_corrupt_key_scopes_fail_safe_without_echoing_storage(
    migrated_path: Path,
) -> None:
    await SQLiteTenantRepository(migrated_path).put(
        TenantRecord(tenant_id="tenant-one", created_at=NOW)
    )
    keys = SQLiteKeyHashRepository(migrated_path)
    await keys.put(
        ApiKeyHashRecord(
            tenant_id="tenant-one",
            key_id="key-one",
            key_hash=b"h" * 32,
            scopes=("runs:read",),
            created_at=NOW,
        )
    )
    sentinel = "credential-private-sentinel"
    with sqlite3.connect(migrated_path) as connection:
        connection.execute(
            "UPDATE api_key_hashes SET scopes = ? WHERE tenant_id = ? AND key_id = ?",
            (f'["{sentinel}"]', "tenant-one", "key-one"),
        )

    with pytest.raises(StorageError) as error:
        await keys.get(tenant_id="tenant-one", key_id="key-one")
    assert sentinel not in str(error.value)


@pytest.mark.asyncio
async def test_key_rotation_requires_the_recorded_predecessor(
    migrated_path: Path,
) -> None:
    await SQLiteTenantRepository(migrated_path).put(
        TenantRecord(tenant_id="tenant-one", created_at=NOW)
    )
    keys = SQLiteKeyHashRepository(migrated_path)
    await keys.put(
        ApiKeyHashRecord(
            tenant_id="tenant-one",
            key_id="key-one",
            key_hash=b"h" * 32,
            created_at=NOW,
        )
    )
    replacement = ApiKeyHashRecord(
        tenant_id="tenant-one",
        key_id="key-two",
        key_hash=b"n" * 32,
        created_at=NOW + timedelta(seconds=1),
        rotated_from_key_id="key-other",
    )

    with pytest.raises(ValueError, match="predecessor"):
        await keys.rotate(
            old_tenant_id="tenant-one",
            old_key_id="key-one",
            new_record=replacement,
            at=NOW + timedelta(seconds=1),
        )
    assert await keys.get(tenant_id="tenant-one", key_id="key-two") is None


def test_repositories_reject_symlink_database_paths(migrated_path: Path) -> None:
    link = migrated_path.with_name("storage-link.sqlite3")
    try:
        link.symlink_to(migrated_path)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with pytest.raises(StorageError, match="regular file"):
        SQLiteTenantRepository(link)


def test_api_key_scope_storage_bound_is_validated_before_sql() -> None:
    scopes = tuple(f"scope{i:02d}" + "a" * 55 for i in range(64))

    with pytest.raises(ValueError, match="scopes exceed"):
        ApiKeyHashRecord(
            tenant_id="tenant-one",
            key_id="key-one",
            key_hash=b"h" * 32,
            scopes=scopes,
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_deleted_tenant_cannot_acquire_new_memory(migrated_path: Path) -> None:
    tenants = SQLiteTenantRepository(migrated_path)
    await tenants.put(TenantRecord(tenant_id="tenant-one", created_at=NOW))
    assert await tenants.delete(tenant_id="tenant-one")

    memory = reflection_repository(migrated_path)
    try:
        with pytest.raises(ReflectionStorageError):
            memory.put(reflection(tenant_id="tenant-one", run_id="run-one"))
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_session_deletion_cascades_runs_events_and_memory(
    migrated_path: Path,
) -> None:
    await seed_session(migrated_path)
    runs = SQLiteRunRepository(migrated_path)
    await runs.create(submission())
    events = SQLiteEventRepository(migrated_path)
    assert len(await events.list(tenant_id="tenant-one", run_id="run-one")) == 1
    memory = reflection_repository(migrated_path)
    memory.put(reflection(tenant_id="tenant-one", run_id="run-one"))
    memory.close()

    assert await SQLiteSessionRepository(migrated_path).delete(
        tenant_id="tenant-one", session_id="session-one"
    )
    assert await runs.get(tenant_id="tenant-one", run_id="run-one") is None
    assert await events.list(tenant_id="tenant-one", run_id="run-one") == ()
    reopened = reflection_repository(migrated_path)
    try:
        assert (
            reopened.list_session(tenant_id="tenant-one", session_id="session-one")
            == ()
        )
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_run_creation_requires_an_existing_tenant_owned_session(
    migrated_path: Path,
) -> None:
    runs = SQLiteRunRepository(migrated_path)

    with pytest.raises(ValueError, match="parent object does not exist"):
        await runs.create(submission())

    await seed_session(migrated_path, tenant_id="tenant-two")
    with pytest.raises(ValueError, match="parent object does not exist"):
        await runs.create(submission())


@pytest.mark.asyncio
async def test_run_creation_does_not_resurrect_a_deleted_session(
    migrated_path: Path,
) -> None:
    await seed_session(migrated_path)
    sessions = SQLiteSessionRepository(migrated_path)
    assert await sessions.delete(tenant_id="tenant-one", session_id="session-one")

    with pytest.raises(ValueError, match="parent object does not exist"):
        await SQLiteRunRepository(migrated_path).create(submission())

    assert await sessions.get(tenant_id="tenant-one", session_id="session-one") is None


@pytest.mark.asyncio
async def test_completed_outcome_and_reflection_survive_reopen(
    migrated_path: Path,
) -> None:
    await seed_session(migrated_path)
    runs = SQLiteRunRepository(migrated_path)
    await runs.create(submission())
    claimed = await runs.claim(
        ClaimRequest(
            tenant_id="tenant-one",
            run_id="run-one",
            worker_id="worker-one",
            lease_id="lease-one",
            now=NOW,
            lease_seconds=30,
        )
    )
    assert claimed.run is not None
    answer = ScopedAnswer(
        answer_text="The public answer.",
        citations=(
            Citation(
                claim="The public claim.",
                evidence_id="ev-public",
                source_url=AnyHttpUrl("https://example.com/report"),
            ),
        ),
    )

    completed = await runs.compare_and_set(
        StateUpdate(
            tenant_id="tenant-one",
            run_id="run-one",
            expected_version=claimed.run.version,
            expected_state=RunState.RUNNING,
            next_state=RunState.COMPLETED,
            lease_id="lease-one",
            worker_id="worker-one",
            at=NOW + timedelta(seconds=1),
            answer=answer,
            reflection=reflection(tenant_id="tenant-one", run_id="run-one"),
        )
    )

    stored = await SQLiteRunRepository(migrated_path).get(
        tenant_id="tenant-one",
        run_id="run-one",
    )
    reopened_memory = reflection_repository(migrated_path)
    try:
        assert completed.run is not None
        assert stored == completed.run
        assert stored is not None and stored.answer == answer
        assert stored.failure_code is None
        assert reopened_memory.get(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-one",
        ) == reflection(tenant_id="tenant-one", run_id="run-one")
        events = await SQLiteEventRepository(migrated_path).list(
            tenant_id="tenant-one", run_id="run-one"
        )
        assert tuple(
            (event.sequence, event.event_type, event.state) for event in events
        ) == (
            (1, RunEventType.STATUS, RunState.QUEUED),
            (2, RunEventType.STATUS, RunState.RUNNING),
            (3, RunEventType.COMPLETED, RunState.COMPLETED),
        )
        assert events[-1].answer == answer
        assert events[-1].failure is None
    finally:
        reopened_memory.close()


@pytest.mark.asyncio
async def test_failed_outcome_and_reflection_survive_reopen(
    migrated_path: Path,
) -> None:
    await seed_session(migrated_path)
    runs = SQLiteRunRepository(migrated_path)
    await runs.create(submission())
    claimed = await runs.claim(
        ClaimRequest(
            tenant_id="tenant-one",
            run_id="run-one",
            worker_id="worker-one",
            lease_id="lease-one",
            now=NOW,
            lease_seconds=30,
        )
    )
    assert claimed.run is not None

    failed = await runs.compare_and_set(
        StateUpdate(
            tenant_id="tenant-one",
            run_id="run-one",
            expected_version=claimed.run.version,
            expected_state=RunState.RUNNING,
            next_state=RunState.FAILED,
            lease_id="lease-one",
            worker_id="worker-one",
            at=NOW + timedelta(seconds=1),
            failure_code=RunFailureCode.EXECUTION_FAILED,
            reflection=failed_reflection(tenant_id="tenant-one", run_id="run-one"),
        )
    )

    stored = await SQLiteRunRepository(migrated_path).get(
        tenant_id="tenant-one",
        run_id="run-one",
    )
    reopened_memory = reflection_repository(migrated_path)
    try:
        assert failed.run is not None
        assert stored == failed.run
        assert stored is not None
        assert stored.failure_code is RunFailureCode.EXECUTION_FAILED
        assert stored.answer is None
        assert reopened_memory.get(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-one",
        ) == failed_reflection(tenant_id="tenant-one", run_id="run-one")
        events = await SQLiteEventRepository(migrated_path).list(
            tenant_id="tenant-one", run_id="run-one"
        )
        assert tuple(event.state for event in events) == (
            RunState.QUEUED,
            RunState.RUNNING,
            RunState.FAILED,
        )
        assert events[-1].failure is not None
        assert events[-1].failure.code is RunFailureCode.EXECUTION_FAILED
        assert events[-1].answer is None
    finally:
        reopened_memory.close()


@pytest.mark.asyncio
async def test_work_queue_receive_is_durable_and_recovers_visibility(
    migrated_path: Path,
) -> None:
    await seed_session(migrated_path)
    await SQLiteRunRepository(migrated_path).create(submission())
    queue = SQLiteWorkQueue(migrated_path)
    item = WorkItem(
        work_id="work-one",
        tenant_id="tenant-one",
        run_id="run-one",
        enqueued_at=NOW,
        not_before=NOW,
    )

    enqueued = await queue.enqueue(item)
    assert enqueued.created is True
    claimed = await queue.receive(now=NOW, visibility_seconds=10)
    reopened = SQLiteWorkQueue(migrated_path)
    hidden = await reopened.receive(
        now=NOW + timedelta(seconds=5), visibility_seconds=10
    )
    recovered = await reopened.receive(
        now=NOW + timedelta(seconds=10), visibility_seconds=10
    )

    assert claimed == enqueued.item
    assert hidden is None
    assert recovered is not None
    assert recovered.work_id == item.work_id
    assert recovered.tenant_id == item.tenant_id
    assert recovered.run_id == item.run_id
    assert recovered.enqueued_at == item.enqueued_at
    assert recovered.not_before == NOW + timedelta(seconds=10)


@pytest.mark.asyncio
async def test_work_queue_receive_is_claimed_once_across_two_consumers(
    migrated_path: Path,
) -> None:
    await seed_session(migrated_path)
    await SQLiteRunRepository(migrated_path).create(submission())
    queue = SQLiteWorkQueue(migrated_path)
    item = WorkItem(
        work_id="work-one",
        tenant_id="tenant-one",
        run_id="run-one",
        enqueued_at=NOW,
        not_before=NOW,
    )
    enqueued = await queue.enqueue(item)

    first, second = await asyncio.gather(
        queue.receive(now=NOW, visibility_seconds=10),
        SQLiteWorkQueue(migrated_path).receive(now=NOW, visibility_seconds=10),
    )

    assert {first, second} == {enqueued.item, None}


@pytest.mark.asyncio
async def test_work_queue_cancel_is_tenant_scoped_after_enqueue(
    migrated_path: Path,
) -> None:
    await seed_session(migrated_path)
    await seed_session(migrated_path, tenant_id="tenant-two")
    runs = SQLiteRunRepository(migrated_path)
    await runs.create(submission())
    await runs.create(
        submission(tenant_id="tenant-two", run_id="run-two", key="request-key-two")
    )
    queue = SQLiteWorkQueue(migrated_path)
    await queue.enqueue(
        WorkItem(
            work_id="work-one",
            tenant_id="tenant-one",
            run_id="run-one",
            enqueued_at=NOW,
            not_before=NOW,
        )
    )
    remaining = await queue.enqueue(
        WorkItem(
            work_id="work-two",
            tenant_id="tenant-two",
            run_id="run-two",
            enqueued_at=NOW,
            not_before=NOW,
        )
    )

    assert await queue.cancel(tenant_id="tenant-one", run_id="run-one") == 1
    assert await queue.receive(now=NOW, visibility_seconds=10) == remaining.item


@pytest.mark.asyncio
async def test_work_queue_rejects_corrupted_stored_items(
    migrated_path: Path,
) -> None:
    await seed_session(migrated_path)
    await SQLiteRunRepository(migrated_path).create(submission())
    queue = SQLiteWorkQueue(migrated_path)
    await queue.enqueue(
        WorkItem(
            work_id="work-one",
            tenant_id="tenant-one",
            run_id="run-one",
            enqueued_at=NOW,
            not_before=NOW,
        )
    )
    with sqlite3.connect(migrated_path) as connection:
        connection.execute(
            "UPDATE work_items SET enqueued_at = ? WHERE work_id = ?",
            ("not-a-timestamp", "work-one"),
        )

    with pytest.raises(StorageError, match="stored work item"):
        await queue.receive(now=NOW, visibility_seconds=10)
