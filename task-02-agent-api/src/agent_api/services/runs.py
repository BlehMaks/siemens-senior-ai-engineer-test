"""Durable run submission and public status projection."""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from datetime import datetime

from search_agent.contracts import OpaqueId, QueryText

from ..ports import (
    TERMINAL_RUN_STATES,
    IdempotencyKey,
    QueueConflictError,
    RunFailureCode,
    RunParentNotFoundError,
    RunRecord,
    RunRepository,
    RunState,
    RunSubmission,
    WorkItem,
    WorkQueue,
)
from ..schemas import RunAcceptedResponse, RunFailure, RunStatusResponse
from ..storage import StorageError
from .sessions import SessionNotFound

_FAILURES: dict[RunFailureCode, tuple[str, bool]] = {
    RunFailureCode.BUDGET_EXHAUSTED: ("Run exhausted its configured budget.", False),
    RunFailureCode.NO_EVIDENCE: ("No sufficient public evidence was found.", False),
    RunFailureCode.SEARCH_FAILED: ("Public evidence search failed.", True),
    RunFailureCode.VALIDATION_FAILED: ("Run output failed validation.", False),
    RunFailureCode.EXECUTION_FAILED: ("Run execution failed.", True),
    RunFailureCode.EXPIRED: ("Run expired before completion.", True),
}


class RunNotFound(LookupError):
    """The run is absent from the authenticated tenant boundary."""


class RunService:
    def __init__(
        self,
        repository: RunRepository,
        queue: WorkQueue,
        *,
        clock: Callable[[], datetime],
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._clock = clock
        self._run_id_factory = _new_run_id if run_id_factory is None else run_id_factory

    async def submit(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        idempotency_key: IdempotencyKey,
        query: QueryText,
    ) -> RunAcceptedResponse:
        now = self._clock()
        try:
            result = await self._repository.create(
                RunSubmission(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    run_id=self._run_id_factory(),
                    idempotency_key=idempotency_key,
                    query=query,
                    created_at=now,
                )
            )
        except RunParentNotFoundError:
            raise SessionNotFound from None

        run = result.run
        if run.state not in TERMINAL_RUN_STATES:
            item = WorkItem(
                work_id=f"work-{run.run_id}",
                tenant_id=run.tenant_id,
                run_id=run.run_id,
                enqueued_at=run.created_at,
                not_before=run.created_at,
            )
            try:
                await self._queue.enqueue(item)
            except RunParentNotFoundError:
                raise SessionNotFound from None
            except QueueConflictError as exc:
                raise StorageError("durable work item conflicts with run") from exc

        # Idempotent retries reproduce the original acceptance even after execution.
        return RunAcceptedResponse(
            session_id=run.session_id,
            run_id=run.run_id,
            state=RunState.QUEUED,
            created_at=run.created_at,
        )

    async def get(self, *, tenant_id: OpaqueId, run_id: OpaqueId) -> RunStatusResponse:
        run = await self._repository.get(tenant_id=tenant_id, run_id=run_id)
        if run is None:
            raise RunNotFound
        return _public_status(run)

    def now(self) -> datetime:
        return self._clock()


def _new_run_id() -> str:
    encoded = base64.b32encode(secrets.token_bytes(16)).decode().lower().rstrip("=")
    return f"run-{encoded}"


def _public_status(run: RunRecord) -> RunStatusResponse:
    failure = None
    if run.failure_code is not None:
        message, retryable = _FAILURES[run.failure_code]
        failure = RunFailure(
            code=run.failure_code,
            message=message,
            retryable=retryable,
        )
    return RunStatusResponse(
        session_id=run.session_id,
        run_id=run.run_id,
        state=run.state,
        created_at=run.created_at,
        updated_at=run.updated_at,
        terminal_at=run.terminal_at,
        cancellation_requested=run.cancellation_requested_at is not None,
        answer=run.answer,
        failure=failure,
    )
