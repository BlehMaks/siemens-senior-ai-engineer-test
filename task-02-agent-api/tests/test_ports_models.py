"""Strict boundary checks for P00 data contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agent_api.ports import (
    ClaimRequest,
    ExecutionLease,
    RunRecord,
    RunState,
    RunSubmission,
    StateUpdate,
    WorkItem,
)
from search_agent.memory import ReflectionRepository, RunReflection

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


def test_memory_port_reuses_task_one_contract_exactly() -> None:
    from agent_api.ports import ReflectionRepository as ApiReflectionRepository

    assert ApiReflectionRepository is ReflectionRepository
    assert RunReflection.__module__.startswith("search_agent.memory")


@pytest.mark.parametrize(
    "timestamp",
    [datetime(2026, 8, 27, 10, 0), NOW.astimezone(timezone(timedelta(hours=2)))],
)
def test_submission_requires_actual_utc_timestamp(timestamp: datetime) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        RunSubmission(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-one",
            idempotency_key="request-key-one",
            query="find the documented answer",
            created_at=timestamp,
        )


def test_submission_rejects_extra_fields_and_non_opaque_ids() -> None:
    values = {
        "tenant_id": "Tenant One",
        "session_id": "session-one",
        "run_id": "run-one",
        "idempotency_key": "request-key-one",
        "query": "find the documented answer",
        "created_at": NOW,
        "unexpected": "value",
    }
    with pytest.raises(ValidationError):
        RunSubmission.model_validate(values)


def test_run_record_lifecycle_fields_are_consistent() -> None:
    base = {
        "tenant_id": "tenant-one",
        "session_id": "session-one",
        "run_id": "run-one",
        "idempotency_key": "request-key-one",
        "query": "find the documented answer",
        "state": RunState.COMPLETED,
        "version": 2,
        "delivery_attempts": 1,
        "created_at": NOW,
        "updated_at": NOW + timedelta(seconds=2),
    }
    with pytest.raises(ValidationError, match="terminal timestamp"):
        RunRecord.model_validate(base)
    with pytest.raises(ValidationError, match="terminal runs cannot retain"):
        RunRecord.model_validate(
            {
                **base,
                "terminal_at": NOW + timedelta(seconds=2),
                "lease": ExecutionLease(
                    lease_id="lease-one",
                    worker_id="worker-one",
                    acquired_at=NOW,
                    expires_at=NOW + timedelta(seconds=30),
                ),
            }
        )


def test_run_record_accepts_explicit_absent_optional_timestamps() -> None:
    record = RunRecord(
        tenant_id="tenant-one",
        session_id="session-one",
        run_id="run-one",
        idempotency_key="request-key-one",
        query="find the documented answer",
        state=RunState.QUEUED,
        version=0,
        delivery_attempts=0,
        created_at=NOW,
        updated_at=NOW,
        cancellation_requested_at=None,
        terminal_at=None,
    )

    assert record.cancellation_requested_at is None
    assert record.terminal_at is None


def test_worker_owned_run_requires_a_lease() -> None:
    with pytest.raises(ValidationError, match="require an execution lease"):
        RunRecord(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-one",
            idempotency_key="request-key-one",
            query="find the documented answer",
            state=RunState.RUNNING,
            version=1,
            delivery_attempts=1,
            created_at=NOW,
            updated_at=NOW,
        )


def test_requested_cancellation_cannot_construct_a_completed_record() -> None:
    with pytest.raises(ValidationError, match="cannot end in another state"):
        RunRecord(
            tenant_id="tenant-one",
            session_id="session-one",
            run_id="run-one",
            idempotency_key="request-key-one",
            query="find the documented answer",
            state=RunState.COMPLETED,
            version=3,
            delivery_attempts=1,
            created_at=NOW,
            updated_at=NOW + timedelta(seconds=2),
            cancellation_requested_at=NOW + timedelta(seconds=1),
            terminal_at=NOW + timedelta(seconds=2),
        )


def test_lease_and_schedule_intervals_are_positive() -> None:
    with pytest.raises(ValidationError, match="lease expiry"):
        ExecutionLease(
            lease_id="lease-one",
            worker_id="worker-one",
            acquired_at=NOW,
            expires_at=NOW,
        )
    with pytest.raises(ValidationError, match="before enqueue"):
        WorkItem(
            work_id="work-one",
            tenant_id="tenant-one",
            run_id="run-one",
            enqueued_at=NOW,
            not_before=NOW - timedelta(microseconds=1),
        )


@pytest.mark.parametrize("lease_seconds", [0, 901])
def test_claim_lease_duration_is_bounded(lease_seconds: int) -> None:
    with pytest.raises(ValidationError):
        ClaimRequest(
            tenant_id="tenant-one",
            run_id="run-one",
            worker_id="worker-one",
            lease_id="lease-one",
            now=NOW,
            lease_seconds=lease_seconds,
        )


def test_state_update_rejects_illegal_or_unowned_transitions() -> None:
    with pytest.raises(ValidationError, match="illegal run state transition"):
        StateUpdate(
            tenant_id="tenant-one",
            run_id="run-one",
            expected_version=1,
            expected_state=RunState.COMPLETED,
            next_state=RunState.RUNNING,
            at=NOW,
        )
    with pytest.raises(ValidationError, match="require exactly one lease"):
        StateUpdate(
            tenant_id="tenant-one",
            run_id="run-one",
            expected_version=1,
            expected_state=RunState.RUNNING,
            next_state=RunState.COMPLETED,
            at=NOW,
        )
