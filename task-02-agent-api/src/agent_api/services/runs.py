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
    RunParentNotFoundError,
    RunRecord,
    RunRepository,
    RunState,
    RunSubmission,
    WorkItem,
    WorkQueue,
)
from ..schemas import (
    CancellationResponse,
    RunAcceptedResponse,
    RunStatusResponse,
    public_run_failure,
)
from ..security.limits import QuotaLimiter, RunAdmission
from ..storage import StorageError
from .sessions import SessionNotFound


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
        limiter: QuotaLimiter | None = None,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._clock = clock
        self._run_id_factory = _new_run_id if run_id_factory is None else run_id_factory
        self._limiter = limiter

    async def submit(
        self,
        *,
        tenant_id: OpaqueId,
        key_id: OpaqueId | None = None,
        session_id: OpaqueId,
        idempotency_key: IdempotencyKey,
        query: QueryText,
    ) -> RunAcceptedResponse:
        now = self._clock()
        run_id = self._run_id_factory()
        admission: RunAdmission | None = None
        if self._limiter is not None:
            if key_id is None:
                raise ValueError("quota-enabled submissions require a key id")
            admission = await self._limiter.admit_run(
                tenant_id=tenant_id,
                key_id=key_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
                query=query,
                run_id=run_id,
                at=now,
            )
            if (
                admission.tenant_id != tenant_id
                or admission.idempotency_key != idempotency_key
            ):
                raise StorageError("quota admission scope is invalid")
            run_id = admission.run_id
        try:
            result = await self._repository.create(
                RunSubmission(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    run_id=run_id,
                    idempotency_key=idempotency_key,
                    query=query,
                    created_at=now,
                )
            )
        except RunParentNotFoundError:
            await _release(self._limiter, admission)
            raise SessionNotFound from None
        except BaseException:
            await _release(self._limiter, admission)
            raise

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

    async def cancel(
        self, *, tenant_id: OpaqueId, run_id: OpaqueId
    ) -> CancellationResponse:
        result = await self._repository.request_cancellation(
            tenant_id=tenant_id,
            run_id=run_id,
            at=self._clock(),
        )
        run = result.run
        if run is None:
            raise RunNotFound
        # Queued and lease-expired cancellations are terminal immediately. Removing
        # their durable dispatch is idempotent, so a retry repairs a prior queue error.
        if run.state is RunState.CANCELLED:
            await self._queue.cancel(tenant_id=tenant_id, run_id=run_id)
        return CancellationResponse(
            run_id=run.run_id,
            state=run.state,
            cancellation_requested=run.cancellation_requested_at is not None,
            changed=result.changed,
            requested_at=run.cancellation_requested_at,
        )

    def now(self) -> datetime:
        return self._clock()


def _new_run_id() -> str:
    encoded = base64.b32encode(secrets.token_bytes(16)).decode().lower().rstrip("=")
    return f"run-{encoded}"


async def _release(
    limiter: QuotaLimiter | None, admission: RunAdmission | None
) -> None:
    if limiter is not None and admission is not None:
        await limiter.release_run(admission)


def _public_status(run: RunRecord) -> RunStatusResponse:
    failure = None
    if run.failure_code is not None:
        failure = public_run_failure(run.failure_code)
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
