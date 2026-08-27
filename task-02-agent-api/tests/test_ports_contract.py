"""Reusable behavior suites for every P00 repository and queue adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from contract_fakes import FakeRunRepository, FakeWorkQueue

from agent_api.ports import (
    ClaimDisposition,
    ClaimRequest,
    IdempotencyConflictError,
    LeaseDisposition,
    LeaseRenewal,
    QueueConflictError,
    RunRepository,
    RunState,
    RunSubmission,
    StateUpdate,
    WorkItem,
    WorkQueue,
    WriteDisposition,
)

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


def submission(
    *,
    tenant_id: str = "tenant-one",
    session_id: str = "session-one",
    run_id: str = "run-one",
    idempotency_key: str = "request-key-one",
    query: str = "find the documented answer",
    created_at: datetime = NOW,
) -> RunSubmission:
    return RunSubmission(
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
        idempotency_key=idempotency_key,
        query=query,
        created_at=created_at,
    )


def claim(
    *,
    tenant_id: str = "tenant-one",
    run_id: str = "run-one",
    worker_id: str = "worker-one",
    lease_id: str = "lease-one",
    now: datetime = NOW,
    lease_seconds: int = 30,
) -> ClaimRequest:
    return ClaimRequest(
        tenant_id=tenant_id,
        run_id=run_id,
        worker_id=worker_id,
        lease_id=lease_id,
        now=now,
        lease_seconds=lease_seconds,
    )


class RunRepositoryContract:
    """Subclass and override the factory to validate another adapter unchanged."""

    repository_factory: Callable[[], RunRepository]

    @pytest.fixture
    def repository(self) -> RunRepository:
        return self.repository_factory()

    @pytest.mark.asyncio
    async def test_create_is_strongly_visible_and_idempotent(
        self, repository: RunRepository
    ) -> None:
        first = await repository.create(submission())
        retry = await repository.create(
            submission(run_id="run-retry", created_at=NOW + timedelta(seconds=5))
        )

        assert first.created is True
        assert retry.created is False
        assert retry.run == first.run
        assert first.run.state is RunState.QUEUED
        assert first.run.version == 0
        assert await repository.get(tenant_id="tenant-one", run_id="run-one") == (
            first.run
        )
        assert await repository.get(tenant_id="tenant-one", run_id="run-retry") is None

    @pytest.mark.asyncio
    async def test_idempotency_is_tenant_scoped_and_payload_bound(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        with pytest.raises(IdempotencyConflictError):
            await repository.create(submission(run_id="run-other", query="different"))

        other_tenant = await repository.create(
            submission(tenant_id="tenant-two", run_id="run-other")
        )
        assert other_tenant.created is True

    @pytest.mark.asyncio
    async def test_session_listing_is_bounded_stable_and_tenant_scoped(
        self, repository: RunRepository
    ) -> None:
        for run in (
            submission(run_id="run-zed"),
            submission(
                run_id="run-alpha",
                idempotency_key="request-key-two",
            ),
            submission(
                tenant_id="tenant-two",
                run_id="run-hidden",
                idempotency_key="request-key-three",
            ),
            submission(
                session_id="session-two",
                run_id="run-other-session",
                idempotency_key="request-key-four",
            ),
        ):
            await repository.create(run)

        listed = await repository.list_session(
            tenant_id="tenant-one", session_id="session-one", limit=1
        )
        assert tuple(run.run_id for run in listed) == ("run-alpha",)
        assert (
            await repository.list_session(
                tenant_id="tenant-three", session_id="session-one"
            )
            == ()
        )
        with pytest.raises(ValueError):
            await repository.list_session(
                tenant_id="tenant-one", session_id="session-one", limit=0
            )

    @pytest.mark.asyncio
    async def test_only_one_concurrent_owner_claims_a_run(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        first, second = await asyncio.gather(
            repository.claim(claim()),
            repository.claim(claim(worker_id="worker-two", lease_id="lease-two")),
        )

        assert {first.disposition, second.disposition} == {
            ClaimDisposition.CLAIMED,
            ClaimDisposition.BUSY,
        }
        stored = await repository.get(tenant_id="tenant-one", run_id="run-one")
        assert stored is not None
        assert stored.delivery_attempts == 1

    @pytest.mark.asyncio
    async def test_duplicate_delivery_is_idempotent_for_the_same_lease(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        first = await repository.claim(claim())
        duplicate = await repository.claim(claim(now=NOW + timedelta(seconds=1)))

        assert first.disposition is ClaimDisposition.CLAIMED
        assert duplicate.disposition is ClaimDisposition.ALREADY_CLAIMED
        assert duplicate.run == first.run

    @pytest.mark.asyncio
    async def test_expired_lease_is_reclaimed_and_old_owner_loses_cas(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        first = await repository.claim(claim(lease_seconds=10))
        reclaimed_at = NOW + timedelta(seconds=10)
        second = await repository.claim(
            claim(
                worker_id="worker-two",
                lease_id="lease-two",
                now=reclaimed_at,
            )
        )

        assert first.run is not None
        assert second.disposition is ClaimDisposition.CLAIMED
        assert second.run is not None
        assert second.run.delivery_attempts == 2
        old_owner = await repository.compare_and_set(
            StateUpdate(
                tenant_id="tenant-one",
                run_id="run-one",
                expected_version=second.run.version,
                expected_state=RunState.RUNNING,
                next_state=RunState.COMPLETED,
                lease_id="lease-one",
                at=reclaimed_at + timedelta(seconds=1),
            )
        )
        assert old_owner.disposition is WriteDisposition.LEASE_LOST

    @pytest.mark.asyncio
    async def test_lease_renewal_extends_ownership_and_rejects_stale_owner(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        claimed = await repository.claim(claim(lease_seconds=10))
        assert claimed.run is not None
        renewed = await repository.renew_lease(
            LeaseRenewal(
                tenant_id="tenant-one",
                run_id="run-one",
                worker_id="worker-one",
                lease_id="lease-one",
                now=NOW + timedelta(seconds=5),
                lease_seconds=30,
            )
        )
        lost = await repository.renew_lease(
            LeaseRenewal(
                tenant_id="tenant-one",
                run_id="run-one",
                worker_id="worker-two",
                lease_id="lease-two",
                now=NOW + timedelta(seconds=6),
                lease_seconds=30,
            )
        )

        assert renewed.disposition is LeaseDisposition.RENEWED
        assert (
            renewed.run is not None and renewed.run.version == claimed.run.version + 1
        )
        assert lost.disposition is LeaseDisposition.LOST

    @pytest.mark.asyncio
    async def test_cas_is_atomic_and_rejects_stale_versions(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        claimed = await repository.claim(claim())
        assert claimed.run is not None
        update = StateUpdate(
            tenant_id="tenant-one",
            run_id="run-one",
            expected_version=claimed.run.version,
            expected_state=RunState.RUNNING,
            next_state=RunState.WAITING_FOR_TOOL,
            lease_id="lease-one",
            at=NOW + timedelta(seconds=1),
        )
        applied, stale = await asyncio.gather(
            repository.compare_and_set(update), repository.compare_and_set(update)
        )

        assert {applied.disposition, stale.disposition} == {
            WriteDisposition.APPLIED,
            WriteDisposition.CONFLICT,
        }

    @pytest.mark.asyncio
    async def test_queued_cancellation_is_immediate_and_idempotent(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        first = await repository.request_cancellation(
            tenant_id="tenant-one", run_id="run-one", at=NOW
        )
        duplicate = await repository.request_cancellation(
            tenant_id="tenant-one", run_id="run-one", at=NOW
        )

        assert first.changed is True
        assert first.run is not None and first.run.state is RunState.CANCELLED
        assert duplicate.changed is False
        assert duplicate.run == first.run
        refused = await repository.claim(claim())
        assert refused.disposition is ClaimDisposition.TERMINAL

    @pytest.mark.asyncio
    async def test_running_cancellation_wins_a_later_completion_race(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        claimed = await repository.claim(claim())
        assert claimed.run is not None
        cancellation = await repository.request_cancellation(
            tenant_id="tenant-one",
            run_id="run-one",
            at=NOW + timedelta(seconds=1),
        )
        assert cancellation.run is not None
        rejected = await repository.compare_and_set(
            StateUpdate(
                tenant_id="tenant-one",
                run_id="run-one",
                expected_version=cancellation.run.version,
                expected_state=RunState.RUNNING,
                next_state=RunState.COMPLETED,
                lease_id="lease-one",
                at=NOW + timedelta(seconds=2),
            )
        )
        cancelled = await repository.compare_and_set(
            StateUpdate(
                tenant_id="tenant-one",
                run_id="run-one",
                expected_version=cancellation.run.version,
                expected_state=RunState.RUNNING,
                next_state=RunState.CANCELLED,
                lease_id="lease-one",
                at=NOW + timedelta(seconds=2),
            )
        )

        assert rejected.disposition is WriteDisposition.CANCELLATION_REQUESTED
        assert cancelled.disposition is WriteDisposition.APPLIED

    @pytest.mark.asyncio
    async def test_completed_run_wins_a_later_cancellation_race(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        claimed = await repository.claim(claim())
        assert claimed.run is not None
        completed = await repository.compare_and_set(
            StateUpdate(
                tenant_id="tenant-one",
                run_id="run-one",
                expected_version=claimed.run.version,
                expected_state=RunState.RUNNING,
                next_state=RunState.COMPLETED,
                lease_id="lease-one",
                at=NOW + timedelta(seconds=1),
            )
        )
        cancellation = await repository.request_cancellation(
            tenant_id="tenant-one",
            run_id="run-one",
            at=NOW + timedelta(seconds=2),
        )

        assert completed.disposition is WriteDisposition.APPLIED
        assert cancellation.changed is False
        assert cancellation.run == completed.run

    @pytest.mark.asyncio
    async def test_cancellation_stops_renewal_and_expired_redelivery(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        await repository.claim(claim(lease_seconds=10))
        await repository.request_cancellation(
            tenant_id="tenant-one",
            run_id="run-one",
            at=NOW + timedelta(seconds=1),
        )
        renewal = await repository.renew_lease(
            LeaseRenewal(
                tenant_id="tenant-one",
                run_id="run-one",
                worker_id="worker-one",
                lease_id="lease-one",
                now=NOW + timedelta(seconds=2),
                lease_seconds=30,
            )
        )
        redelivery = await repository.claim(
            claim(
                worker_id="worker-two",
                lease_id="lease-two",
                now=NOW + timedelta(seconds=10),
            )
        )

        assert renewal.disposition is LeaseDisposition.CANCELLATION_REQUESTED
        assert redelivery.disposition is ClaimDisposition.CANCELLATION_REQUESTED
        assert redelivery.run is not None
        assert redelivery.run.state is RunState.CANCELLED

    @pytest.mark.asyncio
    async def test_tenant_predicates_hide_reads_claims_cancels_and_deletes(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())

        assert await repository.get(tenant_id="tenant-two", run_id="run-one") is None
        hidden_claim = await repository.claim(claim(tenant_id="tenant-two"))
        assert hidden_claim.disposition is ClaimDisposition.NOT_FOUND
        hidden_update = await repository.compare_and_set(
            StateUpdate(
                tenant_id="tenant-two",
                run_id="run-one",
                expected_version=0,
                expected_state=RunState.QUEUED,
                next_state=RunState.FAILED,
                at=NOW,
            )
        )
        assert hidden_update.disposition is WriteDisposition.NOT_FOUND
        assert (
            await repository.request_cancellation(
                tenant_id="tenant-two", run_id="run-one", at=NOW
            )
        ).run is None
        assert (
            await repository.delete_run(tenant_id="tenant-two", run_id="run-one")
            is False
        )
        assert await repository.get(tenant_id="tenant-one", run_id="run-one")

    @pytest.mark.asyncio
    async def test_deletion_scopes_counts_and_releases_idempotency_keys(
        self, repository: RunRepository
    ) -> None:
        await repository.create(submission())
        await repository.create(
            submission(run_id="run-two", idempotency_key="request-key-two")
        )
        await repository.create(
            submission(
                tenant_id="tenant-two",
                run_id="run-three",
                idempotency_key="request-key-three",
            )
        )

        assert (
            await repository.delete_run(tenant_id="tenant-one", run_id="run-one")
            is True
        )
        assert (
            await repository.delete_run(tenant_id="tenant-one", run_id="run-one")
            is False
        )
        replacement = await repository.create(submission(run_id="run-replacement"))
        assert replacement.created is True
        assert (
            await repository.delete_session(
                tenant_id="tenant-one", session_id="session-one"
            )
            == 2
        )
        assert await repository.delete_tenant(tenant_id="tenant-two") == 1
        assert await repository.delete_tenant(tenant_id="tenant-two") == 0


class TestFakeRunRepository(RunRepositoryContract):
    repository_factory = FakeRunRepository


class WorkQueueContract:
    queue_factory: Callable[[], WorkQueue]

    @pytest.fixture
    def queue(self) -> WorkQueue:
        return self.queue_factory()

    @pytest.mark.asyncio
    async def test_enqueue_is_idempotent_but_work_id_is_payload_bound(
        self, queue: WorkQueue
    ) -> None:
        item = WorkItem(
            work_id="work-one",
            tenant_id="tenant-one",
            run_id="run-one",
            enqueued_at=NOW,
            not_before=NOW,
        )
        first = await queue.enqueue(item)
        retry = await queue.enqueue(
            item.model_copy(
                update={
                    "enqueued_at": NOW + timedelta(seconds=1),
                    "not_before": NOW + timedelta(seconds=1),
                }
            )
        )

        assert first.created is True
        assert retry.created is False
        assert retry.item == item
        with pytest.raises(QueueConflictError):
            await queue.enqueue(item.model_copy(update={"run_id": "run-two"}))

    @pytest.mark.asyncio
    async def test_cancel_is_tenant_scoped_and_idempotent(
        self, queue: WorkQueue
    ) -> None:
        for work_id, tenant_id in (
            ("work-one", "tenant-one"),
            ("work-two", "tenant-one"),
            ("work-three", "tenant-two"),
        ):
            await queue.enqueue(
                WorkItem(
                    work_id=work_id,
                    tenant_id=tenant_id,
                    run_id="run-one",
                    enqueued_at=NOW,
                    not_before=NOW,
                )
            )

        assert await queue.cancel(tenant_id="tenant-one", run_id="run-one") == 2
        assert await queue.cancel(tenant_id="tenant-one", run_id="run-one") == 0
        assert await queue.cancel(tenant_id="tenant-two", run_id="run-one") == 1


class TestFakeWorkQueue(WorkQueueContract):
    queue_factory = FakeWorkQueue


@pytest.mark.asyncio
async def test_fake_queue_debug_view_is_deterministic() -> None:
    queue = FakeWorkQueue()
    for work_id, delay in (("work-zed", 0), ("work-alpha", 0), ("work-later", 5)):
        await queue.enqueue(
            WorkItem(
                work_id=work_id,
                tenant_id="tenant-one",
                run_id=f"run-{work_id}",
                enqueued_at=NOW,
                not_before=NOW + timedelta(seconds=delay),
            )
        )

    assert tuple(item.work_id for item in await queue.ordered_items()) == (
        "work-alpha",
        "work-zed",
        "work-later",
    )
