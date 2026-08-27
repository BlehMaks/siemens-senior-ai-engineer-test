"""Signed internal task delivery for Cloud Tasks worker invocation."""

from __future__ import annotations

from typing import Protocol, cast

from fastapi import APIRouter, Request, Response

from ..ports import WorkItem
from ..schemas import ErrorEnvelope
from ..storage import CloudTasksWorkQueue, TaskDeliveryAuthError


class TaskDeliveryWorker(Protocol):
    async def process(self, item: WorkItem) -> None: ...


def build_internal_task_router(*, path: str) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.post(
        path,
        status_code=204,
        response_class=Response,
        responses={401: {"model": ErrorEnvelope}, 503: {"model": ErrorEnvelope}},
        tags=["internal"],
    )
    async def deliver(request: Request) -> Response:
        queue = cast(CloudTasksWorkQueue, request.app.state.work_queue)
        worker = cast(TaskDeliveryWorker | None, request.app.state.internal_worker)
        if worker is None:
            raise RuntimeError("task delivery worker is unavailable")
        body = await request.body()
        item = queue.decode_delivery(
            body=body,
            signature=_single_header(request, "x-agent-api-task-signature"),
            task_name=_single_header(request, "x-cloudtasks-taskname"),
            queue_name=_single_header(request, "x-cloudtasks-queuename"),
        )
        await worker.process(item)
        return Response(status_code=204)

    return router


def _single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    return values[0] if len(values) == 1 else None


__all__ = ["TaskDeliveryAuthError", "TaskDeliveryWorker", "build_internal_task_router"]
