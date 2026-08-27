from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import AnyHttpUrl

from agent_api.ports import RunState, RunSubmission
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
    StorageError,
    TenantRecord,
    reflection_repository,
)
from search_agent.contracts import Citation, ScopedAnswer, TerminalState
from search_agent.memory import (
    CompletionEvidence,
    ReflectionStorageError,
    ReflectionUsage,
    RunReflection,
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
        actions=(),
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


@pytest.mark.asyncio
async def test_run_and_event_order_survive_reopen(migrated_path: Path) -> None:
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
    for sequence in (3, 1, 2):
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
    ) == (1, 2, 3)
    assert await reopened_events.list(tenant_id="tenant-two", run_id="run-alpha") == ()

    completed = RunEvent(
        sequence=4,
        run_id="run-alpha",
        event_type=RunEventType.COMPLETED,
        state=RunState.COMPLETED,
        occurred_at=NOW + timedelta(seconds=4),
        message="Run completed.",
        answer=ScopedAnswer(
            answer_text="The public answer.",
            citations=(
                Citation(
                    claim="The public claim.",
                    evidence_id="ev-public",
                    source_url=AnyHttpUrl("https://example.com/report"),
                ),
            ),
        ),
    )
    assert await reopened_events.append(tenant_id="tenant-one", event=completed)
    assert (
        await reopened_events.list(
            tenant_id="tenant-one", run_id="run-alpha", after_sequence=3
        )
    ) == (completed,)


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


def test_repositories_reject_symlink_database_paths(migrated_path: Path) -> None:
    link = migrated_path.with_name("storage-link.sqlite3")
    try:
        link.symlink_to(migrated_path)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with pytest.raises(StorageError, match="regular file"):
        SQLiteTenantRepository(link)


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
    runs = SQLiteRunRepository(migrated_path)
    await runs.create(submission())
    events = SQLiteEventRepository(migrated_path)
    await events.append(
        tenant_id="tenant-one",
        event=RunEvent(
            sequence=1,
            run_id="run-one",
            event_type=RunEventType.STATUS,
            state=RunState.QUEUED,
            occurred_at=NOW,
            message="Run accepted.",
        ),
    )
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
