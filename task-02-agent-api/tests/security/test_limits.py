from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_api.ports import IdempotencyConflictError, RunSubmission
from agent_api.security import (
    ApiKeyManager,
    LimitConfig,
    QuotaExceeded,
    SQLiteQuotaLimiter,
)
from agent_api.storage import (
    SessionRecord,
    SQLiteKeyHashRepository,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTenantRepository,
    StorageError,
    TenantRecord,
    migrate,
)

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


async def seed_identity(path: Path, *, tenant_id: str, key_id_hint: str) -> str:
    await SQLiteTenantRepository(path).put(
        TenantRecord(tenant_id=tenant_id, created_at=NOW)
    )
    generated = await ApiKeyManager(
        SQLiteKeyHashRepository(path),
        FixedPepper(),
    ).create(tenant_id=tenant_id, scopes=("runs:read",), now=NOW)
    assert generated.record.key_id != key_id_hint
    return generated.record.key_id


async def seed_run(path: Path, *, tenant_id: str, run_id: str) -> None:
    sessions = SQLiteSessionRepository(path)
    await sessions.put(
        SessionRecord(
            tenant_id=tenant_id,
            session_id="session-one",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await SQLiteRunRepository(path).create(
        RunSubmission(
            tenant_id=tenant_id,
            session_id="session-one",
            run_id=run_id,
            idempotency_key=f"request-{run_id}",
            query="Find the documented answer.",
            created_at=NOW,
        )
    )


@pytest.mark.asyncio
async def test_token_bucket_is_durable_isolated_and_returns_exact_retry_after(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rate.sqlite3"
    await migrate(path)
    key_one = await seed_identity(path, tenant_id="tenant-one", key_id_hint="one")
    key_two = await seed_identity(path, tenant_id="tenant-one", key_id_hint="two")
    foreign = await seed_identity(path, tenant_id="tenant-two", key_id_hint="foreign")
    config = LimitConfig(request_burst=2, requests_per_second=0.5)
    limiter = SQLiteQuotaLimiter(path, config)

    await limiter.admit_request(tenant_id="tenant-one", key_id=key_one, at=NOW)
    await limiter.admit_request(tenant_id="tenant-one", key_id=key_one, at=NOW)
    with pytest.raises(QuotaExceeded) as blocked:
        await SQLiteQuotaLimiter(path, config).admit_request(
            tenant_id="tenant-one", key_id=key_one, at=NOW
        )
    assert blocked.value.retry_after == 2

    await limiter.admit_request(tenant_id="tenant-one", key_id=key_two, at=NOW)
    await limiter.admit_request(tenant_id="tenant-two", key_id=foreign, at=NOW)
    await limiter.admit_request(
        tenant_id="tenant-one", key_id=key_one, at=NOW + timedelta(seconds=2)
    )


@pytest.mark.asyncio
async def test_run_admission_is_atomic_idempotent_and_rolls_over_daily(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.sqlite3"
    await migrate(path)
    key = await seed_identity(path, tenant_id="tenant-one", key_id_hint="one")
    config = LimitConfig(max_queued_runs=1, daily_work_units=1)
    limiter = SQLiteQuotaLimiter(path, config)
    arguments = {
        "tenant_id": "tenant-one",
        "key_id": key,
        "session_id": "session-one",
        "idempotency_key": "request-key-one",
        "query": "Find the documented answer.",
        "run_id": "run-one",
        "at": NOW,
    }

    first, retry = await asyncio.gather(
        limiter.admit_run(**arguments),
        SQLiteQuotaLimiter(path, config).admit_run(**arguments),
    )
    assert {first.created, retry.created} == {True, False}
    with pytest.raises(IdempotencyConflictError):
        await limiter.admit_run(**{**arguments, "query": "Find another answer."})
    with pytest.raises(QuotaExceeded) as queued:
        await limiter.admit_run(
            **{
                **arguments,
                "idempotency_key": "request-key-two",
                "run_id": "run-two",
            }
        )
    assert queued.value.retry_after == 1

    await limiter.release_run(first if first.created else retry)
    admitted = await limiter.admit_run(
        **{
            **arguments,
            "idempotency_key": "request-key-two",
            "run_id": "run-two",
        }
    )
    await seed_run(path, tenant_id="tenant-one", run_id="run-two")
    daily_limiter = SQLiteQuotaLimiter(
        path, LimitConfig(max_queued_runs=10, daily_work_units=1)
    )
    with pytest.raises(QuotaExceeded) as daily:
        await daily_limiter.admit_run(
            **{
                **arguments,
                "idempotency_key": "request-key-three",
                "run_id": "run-three",
            }
        )
    assert daily.value.retry_after == 14 * 60 * 60

    next_day = NOW + timedelta(days=1)
    rolled = await daily_limiter.admit_run(
        **{
            **arguments,
            "idempotency_key": "request-key-three",
            "run_id": "run-three",
            "at": next_day,
        }
    )
    assert admitted.created and rolled.created


@pytest.mark.asyncio
async def test_execution_and_sse_admission_are_atomic_tenant_caps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "live.sqlite3"
    await migrate(path)
    key_one = await seed_identity(path, tenant_id="tenant-one", key_id_hint="one")
    key_two = await seed_identity(path, tenant_id="tenant-one", key_id_hint="two")
    foreign = await seed_identity(path, tenant_id="tenant-two", key_id_hint="foreign")
    await seed_run(path, tenant_id="tenant-one", run_id="run-one")
    await seed_run(path, tenant_id="tenant-one", run_id="run-two")
    config = LimitConfig(max_concurrent_runs=1, max_sse_connections=1)
    limiter = SQLiteQuotaLimiter(path, config)

    permits = await asyncio.gather(
        limiter.acquire_execution(
            tenant_id="tenant-one", run_id="run-one", at=NOW, lease_seconds=5
        ),
        SQLiteQuotaLimiter(path, config).acquire_execution(
            tenant_id="tenant-one", run_id="run-two", at=NOW, lease_seconds=5
        ),
    )
    assert sum(permit is not None for permit in permits) == 1
    blocked_run = "run-one" if permits[0] is None else "run-two"
    live = permits[0] if permits[0] is not None else permits[1]
    assert live is not None
    assert await limiter.renew_execution(
        live, at=NOW + timedelta(seconds=4), lease_seconds=5
    )
    still_blocked = await limiter.acquire_execution(
        tenant_id="tenant-one",
        run_id=blocked_run,
        at=NOW + timedelta(seconds=5),
        lease_seconds=5,
    )
    assert still_blocked is None
    recovered = await limiter.acquire_execution(
        tenant_id="tenant-one",
        run_id=blocked_run,
        at=NOW + timedelta(seconds=9),
        lease_seconds=5,
    )
    assert recovered is not None

    stream = await limiter.acquire_sse(tenant_id="tenant-one", key_id=key_one, at=NOW)
    with pytest.raises(QuotaExceeded):
        await SQLiteQuotaLimiter(path, config).acquire_sse(
            tenant_id="tenant-one", key_id=key_two, at=NOW
        )
    foreign_stream = await limiter.acquire_sse(
        tenant_id="tenant-two", key_id=foreign, at=NOW
    )
    assert await limiter.renew_sse(stream, at=NOW + timedelta(seconds=20))
    await limiter.release_sse(stream)
    reconnected = await limiter.acquire_sse(
        tenant_id="tenant-one", key_id=key_two, at=NOW + timedelta(seconds=20)
    )
    await limiter.release_sse(reconnected)
    await limiter.release_sse(foreign_stream)


@pytest.mark.asyncio
async def test_expired_execution_and_sse_leases_cannot_be_renewed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "expired-live.sqlite3"
    await migrate(path)
    key = await seed_identity(path, tenant_id="tenant-one", key_id_hint="one")
    for run_id in ("run-before", "run-exact", "run-after"):
        await seed_run(path, tenant_id="tenant-one", run_id=run_id)
    limiter = SQLiteQuotaLimiter(
        path, LimitConfig(max_concurrent_runs=3, max_sse_connections=3)
    )
    executions = [
        await limiter.acquire_execution(
            tenant_id="tenant-one", run_id=run_id, at=NOW, lease_seconds=5
        )
        for run_id in ("run-before", "run-exact", "run-after")
    ]
    streams = [
        await limiter.acquire_sse(tenant_id="tenant-one", key_id=key, at=NOW)
        for _ in range(3)
    ]
    assert all(permit is not None for permit in executions)
    before, exact, after = executions
    assert before is not None and exact is not None and after is not None

    assert await limiter.renew_execution(
        before, at=NOW + timedelta(seconds=4), lease_seconds=5
    )
    assert not await limiter.renew_execution(
        exact, at=NOW + timedelta(seconds=5), lease_seconds=5
    )
    assert not await limiter.renew_execution(
        after, at=NOW + timedelta(seconds=6), lease_seconds=5
    )
    assert await limiter.renew_sse(streams[0], at=NOW + timedelta(seconds=29))
    assert not await limiter.renew_sse(streams[1], at=NOW + timedelta(seconds=30))
    assert not await limiter.renew_sse(streams[2], at=NOW + timedelta(seconds=31))


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["revoked", "expired"])
async def test_inactive_key_cannot_renew_an_existing_sse_lease(
    tmp_path: Path,
    lifecycle: str,
) -> None:
    path = tmp_path / "inactive-key-stream.sqlite3"
    await migrate(path)
    await SQLiteTenantRepository(path).put(
        TenantRecord(tenant_id="tenant-one", created_at=NOW)
    )
    manager = ApiKeyManager(SQLiteKeyHashRepository(path), FixedPepper())
    generated = await manager.create(
        tenant_id="tenant-one",
        scopes=("runs:read",),
        now=NOW,
        expires_at=NOW + timedelta(seconds=10),
    )
    limiter = SQLiteQuotaLimiter(path, LimitConfig())
    permit = await limiter.acquire_sse(
        tenant_id="tenant-one",
        key_id=generated.record.key_id,
        at=NOW,
    )
    if lifecycle == "revoked":
        assert await manager.revoke(
            authorization=f"Bearer {generated.plaintext}",
            now=NOW + timedelta(seconds=1),
        )

    assert not await limiter.renew_sse(permit, at=NOW + timedelta(seconds=20))


@pytest.mark.asyncio
async def test_accounting_storage_failure_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "outage.sqlite3"
    await migrate(path)
    key = await seed_identity(path, tenant_id="tenant-one", key_id_hint="one")
    limiter = SQLiteQuotaLimiter(path, LimitConfig())
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE quota_rate_buckets")

    with pytest.raises(StorageError, match="accounting failed"):
        await limiter.admit_request(tenant_id="tenant-one", key_id=key, at=NOW)


@pytest.mark.asyncio
async def test_orphan_admission_ttl_frees_queue_without_refunding_daily_units(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orphan.sqlite3"
    await migrate(path)
    key = await seed_identity(path, tenant_id="tenant-one", key_id_hint="one")
    config = LimitConfig(
        max_queued_runs=1,
        daily_work_units=100,
        pending_admission_seconds=5,
    )
    limiter = SQLiteQuotaLimiter(path, config)
    common = {
        "tenant_id": "tenant-one",
        "key_id": key,
        "session_id": "session-one",
        "query": "Find the documented answer.",
    }
    await limiter.admit_run(
        **common,
        idempotency_key="request-key-one",
        run_id="run-one",
        at=NOW,
    )
    await seed_run(path, tenant_id="tenant-one", run_id="run-one")
    assert await SQLiteRunRepository(path).delete_run(
        tenant_id="tenant-one", run_id="run-one"
    )

    with pytest.raises(QuotaExceeded):
        await limiter.admit_run(
            **common,
            idempotency_key="request-key-two",
            run_id="run-two",
            at=NOW + timedelta(seconds=4),
        )
    second = await limiter.admit_run(
        **common,
        idempotency_key="request-key-two",
        run_id="run-two",
        at=NOW + timedelta(seconds=5),
    )
    assert second.created

    daily_limiter = SQLiteQuotaLimiter(
        path,
        LimitConfig(
            max_queued_runs=10,
            daily_work_units=2,
            pending_admission_seconds=5,
        ),
    )
    with pytest.raises(QuotaExceeded) as daily:
        await daily_limiter.admit_run(
            **common,
            idempotency_key="request-key-three",
            run_id="run-three",
            at=NOW + timedelta(seconds=5),
        )
    assert daily.value.retry_after == 14 * 60 * 60 - 5

    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM api_key_hashes WHERE tenant_id = ? AND key_id = ?",
            ("tenant-one", key),
        )
    replacement = await seed_identity(
        path, tenant_id="tenant-one", key_id_hint="replacement"
    )
    retry = await daily_limiter.admit_run(
        **{**common, "key_id": replacement},
        idempotency_key="request-key-two",
        run_id="run-discarded",
        at=NOW + timedelta(seconds=5),
    )
    assert retry.created is False and retry.run_id == "run-two"


@pytest.mark.parametrize(
    "changes",
    [
        {"max_request_bytes": 0},
        {"request_burst": True},
        {"requests_per_second": float("nan")},
        {"max_queued_runs": -1},
        {"sse_lease_seconds": 29},
    ],
)
def test_limit_config_rejects_unsafe_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        LimitConfig(**changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("retry_after", [0, -1, True, 1.0])
def test_quota_error_rejects_invalid_retry_after(retry_after: object) -> None:
    with pytest.raises(ValueError, match="Retry-After"):
        QuotaExceeded(retry_after=retry_after)  # type: ignore[arg-type]
