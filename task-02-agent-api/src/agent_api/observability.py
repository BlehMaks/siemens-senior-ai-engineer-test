"""Privacy-preserving operational signals with bounded metric dimensions."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import aiosqlite

from search_agent import RunUsage
from search_agent.contracts import OpaqueId

from .ports import RunFailureCode, RunState
from .storage import AuditEntry

_LOGGER = logging.getLogger("agent_api.operations")
_TERMINAL_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.EXPIRED}
)
_FAILURE_LABELS = frozenset({"none", *(code.value for code in RunFailureCode)})
_WORK_OUTCOMES = frozenset(
    {
        "claimed",
        "busy",
        "cancelled",
        "completed",
        "failed",
        "not_found",
        "quota_blocked",
        "terminal",
    }
)
_LEASE_OUTCOMES = frozenset({"acquired", "blocked", "lost", "released", "renewed"})
_USAGE_FIELDS = (
    "iterations",
    "search_queries",
    "pages",
    "failed_pages",
    "raw_bytes_reserved",
    "decoded_bytes",
    "model_calls",
    "model_attempts",
    "tokens",
)
_METRIC_SCHEMA: dict[str, dict[str, frozenset[str]]] = {
    "api_runs_submitted_total": {},
    "api_run_cancellations_total": {"changed": frozenset({"true", "false"})},
    "api_runs_terminal_total": {
        "state": frozenset(state.value for state in _TERMINAL_STATES),
        "failure": _FAILURE_LABELS,
    },
    "api_run_elapsed_seconds_total": {
        "state": frozenset(state.value for state in _TERMINAL_STATES)
    },
    "api_policy_failures_total": {"reason": _FAILURE_LABELS - {"none"}},
    "api_run_usage_total": {"resource": frozenset(_USAGE_FIELDS)},
    "api_worker_outcomes_total": {"outcome": _WORK_OUTCOMES},
    "api_lease_outcomes_total": {"outcome": _LEASE_OUTCOMES},
    "api_readiness_checks_total": {"outcome": frozenset({"ready", "not_ready"})},
    "api_telemetry_failures_total": {"component": frozenset({"audit", "logging"})},
}


class AuditWriter(Protocol):
    async def append(self, entry: AuditEntry) -> bool: ...


class ReadinessProbe(Protocol):
    async def ready(self) -> bool: ...


class SQLiteReadinessProbe:
    """Read-only dependency check for the local durable runtime."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def ready(self) -> bool:
        if self._path.is_symlink() or not self._path.is_file():
            return False
        try:
            async with aiosqlite.connect(
                f"{self._path.resolve().as_uri()}?mode=ro", uri=True
            ) as connection:
                for table in (
                    "schema_migrations",
                    "runs",
                    "work_items",
                    "quota_rate_buckets",
                ):
                    await (
                        await connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1')
                    ).fetchone()
        except (OSError, sqlite3.Error):
            return False
        return True


@dataclass(frozen=True, slots=True)
class MetricSample:
    name: str
    labels: tuple[tuple[str, str], ...]
    value: float


class OperationalTelemetry:
    """Emit only typed safe fields; metric key space is finite by construction."""

    def __init__(
        self,
        *,
        pseudonym_key: bytes,
        audit: AuditWriter,
        logger: logging.Logger | None = None,
    ) -> None:
        if type(pseudonym_key) is not bytes or len(pseudonym_key) < 32:
            raise ValueError("telemetry pseudonym key must contain at least 32 bytes")
        self._key = pseudonym_key
        self._audit = audit
        self._logger = _LOGGER if logger is None else logger
        self._metrics: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._metric_lock = threading.Lock()

    async def run_submitted(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        correlation_id: OpaqueId,
        at: datetime,
    ) -> None:
        self._increment("api_runs_submitted_total")
        self._emit(
            "run.submitted",
            at=at,
            tenant=self._pseudonym("tenant", tenant_id),
            session=self._pseudonym("session", session_id),
            run=self._pseudonym("run", run_id),
            correlation_id=correlation_id,
        )
        await self._append_audit(tenant_id, "run.submitted", run_id, at)

    async def run_cancelled(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        state: RunState,
        changed: bool,
        correlation_id: OpaqueId,
        at: datetime,
    ) -> None:
        self._increment("api_run_cancellations_total", changed=str(changed).lower())
        self._emit(
            "run.cancellation_requested",
            at=at,
            tenant=self._pseudonym("tenant", tenant_id),
            run=self._pseudonym("run", run_id),
            state=state.value,
            changed=changed,
            correlation_id=correlation_id,
        )
        await self._append_audit(tenant_id, "run.cancellation-requested", run_id, at)

    async def run_terminal(
        self,
        *,
        tenant_id: OpaqueId,
        session_id: OpaqueId,
        run_id: OpaqueId,
        state: RunState,
        failure_code: RunFailureCode | None,
        usage: RunUsage | None,
        at: datetime,
    ) -> None:
        if state not in _TERMINAL_STATES:
            raise ValueError("terminal telemetry requires a terminal run state")
        failure = "none" if failure_code is None else failure_code.value
        self._increment("api_runs_terminal_total", state=state.value, failure=failure)
        if failure_code is not None:
            self._increment("api_policy_failures_total", reason=failure)
        usage_fields: dict[str, float | int] = {}
        if usage is not None:
            self._increment(
                "api_run_elapsed_seconds_total",
                usage.elapsed_seconds,
                state=state.value,
            )
            usage_fields = {name: getattr(usage, name) for name in _USAGE_FIELDS}
            for resource, amount in usage_fields.items():
                self._increment("api_run_usage_total", amount, resource=resource)
        self._emit(
            "run.terminal",
            at=at,
            tenant=self._pseudonym("tenant", tenant_id),
            session=self._pseudonym("session", session_id),
            run=self._pseudonym("run", run_id),
            state=state.value,
            failure=failure,
            **({"elapsed_seconds": usage.elapsed_seconds} if usage is not None else {}),
            **usage_fields,
        )
        await self._append_audit(tenant_id, f"run.{state.value}", run_id, at)

    def work_outcome(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        outcome: str,
        at: datetime,
    ) -> None:
        if outcome not in _WORK_OUTCOMES:
            raise ValueError("worker outcome is not a bounded telemetry label")
        self._increment("api_worker_outcomes_total", outcome=outcome)
        self._emit(
            "worker.outcome",
            at=at,
            tenant=self._pseudonym("tenant", tenant_id),
            run=self._pseudonym("run", run_id),
            outcome=outcome,
        )

    def lease_outcome(
        self,
        *,
        tenant_id: OpaqueId,
        run_id: OpaqueId,
        outcome: str,
        at: datetime,
    ) -> None:
        if outcome not in _LEASE_OUTCOMES:
            raise ValueError("lease outcome is not a bounded telemetry label")
        self._increment("api_lease_outcomes_total", outcome=outcome)
        self._emit(
            "worker.lease",
            at=at,
            tenant=self._pseudonym("tenant", tenant_id),
            run=self._pseudonym("run", run_id),
            outcome=outcome,
        )

    def readiness(self, *, ready: bool, at: datetime) -> None:
        outcome = "ready" if ready else "not_ready"
        self._increment("api_readiness_checks_total", outcome=outcome)
        self._emit("service.readiness", at=at, outcome=outcome)

    def snapshot(self) -> tuple[MetricSample, ...]:
        with self._metric_lock:
            values = tuple(self._metrics.items())
        return tuple(
            MetricSample(name=name, labels=labels, value=value)
            for (name, labels), value in sorted(values)
        )

    async def _append_audit(
        self,
        tenant_id: OpaqueId,
        action: str,
        subject_id: OpaqueId,
        at: datetime,
    ) -> None:
        try:
            await self._audit.append(
                AuditEntry(
                    tenant_id=tenant_id,
                    entry_id=self._pseudonym("audit", f"{action}-{subject_id}"),
                    action=action,
                    occurred_at=at,
                )
            )
        except Exception:
            self._increment("api_telemetry_failures_total", component="audit")

    def _pseudonym(self, kind: str, value: OpaqueId) -> str:
        message = f"agent-api-telemetry-v1\0{kind}\0{value}".encode()
        digest = hmac.new(self._key, message, hashlib.sha256).hexdigest()[:24]
        return f"{kind}-{digest}"

    def _increment(self, name: str, amount: float | int = 1, **labels: str) -> None:
        checked = _metric_key(name, labels)
        value = float(amount)
        if not math.isfinite(value) or value < 0:
            raise ValueError("metric increments must be non-negative")
        with self._metric_lock:
            self._metrics[checked] = self._metrics.get(checked, 0.0) + value

    def _emit(self, event: str, *, at: datetime, **fields: object) -> None:
        payload = {
            "event": event,
            "occurred_at": _timestamp(at),
            **fields,
        }
        try:
            self._logger.info(
                json.dumps(
                    payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
                )
            )
        except Exception:
            self._increment("api_telemetry_failures_total", component="logging")


def _metric_key(
    name: str, labels: dict[str, str]
) -> tuple[str, tuple[tuple[str, str], ...]]:
    schema = _METRIC_SCHEMA.get(name)
    if schema is None or labels.keys() != schema.keys():
        raise ValueError("metric name or labels are not allowlisted")
    if any(value not in schema[key] for key, value in labels.items()):
        raise ValueError("metric label value is not allowlisted")
    return name, tuple(sorted(labels.items()))


def _timestamp(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("telemetry timestamp must be UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError("telemetry timestamp must be UTC")
    return value.isoformat(timespec="microseconds")


__all__ = [
    "MetricSample",
    "OperationalTelemetry",
    "ReadinessProbe",
    "SQLiteReadinessProbe",
]
