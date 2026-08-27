"""Derive and persist bounded retrospectives from public run observations."""

from __future__ import annotations

import re
import sqlite3
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

from pydantic import TypeAdapter, ValidationError

from ..contracts import (
    Citation,
    ExtractedEvidence,
    FailureReason,
    OpaqueId,
    PublicEvent,
    ScopedAnswer,
    SearchHit,
    TerminalState,
)
from ..runner import RunResult, RunUsage
from ..security import SitePolicy
from ..state import RunSnapshot
from .contracts import (
    CompletionEvidence,
    FailureCode,
    ObservedFailure,
    RecoveryStep,
    ReflectionInputError,
    ReflectionStorageError,
    ReflectionUsage,
    RepositoryClosedError,
    RunReflection,
    UnresolvedItem,
    contains_sensitive_memory_hostname,
    contains_sensitive_memory_text,
    redact_memory_text,
)

_ID_ADAPTER = TypeAdapter(OpaqueId)
_MAX_SERIALIZED_BYTES = 64 * 1024
_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "key",
        "passwd",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
)
_SENSITIVE_QUERY_SUFFIXES = (
    "_access_token",
    "_api_key",
    "_secret",
    "_signature",
    "_token",
)
# A valid plan can issue at most eight searches with five hits per search.
_MAX_RUN_HITS = 40
_FAILURE_CODES = {
    FailureReason.BUDGET_EXHAUSTED: FailureCode.BUDGET_EXHAUSTED,
    FailureReason.CANCELLED: FailureCode.CANCELLED,
    FailureReason.NO_EVIDENCE: FailureCode.NO_EVIDENCE,
    FailureReason.SEARCH_FAILED: FailureCode.SEARCH_FAILED,
    FailureReason.VALIDATION_FAILED: FailureCode.VALIDATION_FAILED,
}
_UNRESOLVED_ITEMS = {
    FailureReason.BUDGET_EXHAUSTED: UnresolvedItem.BUDGET_EXHAUSTED,
    FailureReason.CANCELLED: UnresolvedItem.CANCELLED,
    FailureReason.NO_EVIDENCE: UnresolvedItem.NO_EVIDENCE,
    FailureReason.SEARCH_FAILED: UnresolvedItem.SEARCH_FAILED,
    FailureReason.VALIDATION_FAILED: UnresolvedItem.VALIDATION_FAILED,
}


def reflect_run(result: RunResult) -> RunReflection:
    """Create one immutable reflection without retaining model or adapter internals."""

    checked = _validate_run_result(result)
    snapshot = checked.snapshot
    outcome = snapshot.terminal_state
    assert outcome is not None

    failures: list[ObservedFailure] = []
    if checked.usage.failed_pages:
        failures.append(
            ObservedFailure(
                code=FailureCode.PAGE_PROCESSING_FAILED,
                count=checked.usage.failed_pages,
            )
        )
    if snapshot.failure_reason is not None:
        failures.append(
            ObservedFailure(code=_FAILURE_CODES[snapshot.failure_reason], count=1)
        )

    completion_evidence = _completion_evidence(snapshot)
    recovery_steps = (
        (RecoveryStep.CONTINUED_WITH_REMAINING_EVIDENCE,)
        if checked.usage.failed_pages and snapshot.evidence
        else ()
    )
    unresolved_items: tuple[UnresolvedItem, ...] = ()
    if outcome is TerminalState.CANCELLED:
        unresolved_items = (UnresolvedItem.CANCELLED,)
    elif snapshot.failure_reason is not None:
        unresolved_items = (_UNRESOLVED_ITEMS[snapshot.failure_reason],)

    return RunReflection(
        tenant_id=snapshot.tenant_id,
        session_id=snapshot.session_id,
        run_id=snapshot.run_id,
        requested_outcome=redact_memory_text(snapshot.request),
        actions=tuple(event.event_type for event in checked.events),
        failures=tuple(failures),
        recovery_steps=recovery_steps,
        completion_evidence=completion_evidence,
        unresolved_items=unresolved_items,
        outcome=outcome,
        usage=ReflectionUsage.model_validate(
            checked.usage.model_dump(mode="python", warnings="error"), strict=True
        ),
    )


class InMemoryReflectionRepository:
    """Small deterministic adapter for tests and single-process local use."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], RunReflection] = {}

    def put(self, reflection: RunReflection) -> None:
        checked = _validate_reflection(reflection)
        self._items[_key(checked.tenant_id, checked.session_id, checked.run_id)] = (
            checked
        )

    def get(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, run_id: OpaqueId
    ) -> RunReflection | None:
        key = _key(tenant_id, session_id, run_id)
        item = self._items.get(key)
        return _validate_stored_reflection(key, item) if item is not None else None

    def list_session(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, limit: int = 100
    ) -> tuple[RunReflection, ...]:
        checked_tenant = _scope_id(tenant_id)
        checked_session = _scope_id(session_id)
        checked_limit = _limit(limit)
        selected = [
            _validate_stored_reflection(key, reflection)
            for key, reflection in self._items.items()
            if key[:2] == (checked_tenant, checked_session)
        ]
        selected.sort(key=lambda reflection: reflection.run_id)
        return tuple(selected[:checked_limit])

    def delete_run(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, run_id: OpaqueId
    ) -> bool:
        return self._items.pop(_key(tenant_id, session_id, run_id), None) is not None

    def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        checked_session = _scope_id(session_id)
        keys = [
            key for key in self._items if key[:2] == (checked_tenant, checked_session)
        ]
        for key in keys:
            del self._items[key]
        return len(keys)

    def delete_tenant(self, *, tenant_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        keys = [key for key in self._items if key[0] == checked_tenant]
        for key in keys:
            del self._items[key]
        return len(keys)


class SQLiteReflectionRepository:
    """Standalone SQLite adapter with a fixed composite tenant key."""

    _CREATE = """
        CREATE TABLE IF NOT EXISTS run_reflections (
            tenant_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            payload TEXT NOT NULL CHECK(length(payload) <= 65536),
            PRIMARY KEY (tenant_id, session_id, run_id)
        ) WITHOUT ROWID
    """
    _EXPECTED_SCHEMA = (
        ("tenant_id", "TEXT", 1, 1),
        ("session_id", "TEXT", 1, 2),
        ("run_id", "TEXT", 1, 3),
        ("payload", "TEXT", 1, 0),
    )

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ReflectionStorageError("SQLite path must be a filesystem path")
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ReflectionStorageError("SQLite path must be a regular file")
        if not path.parent.is_dir():
            raise ReflectionStorageError("SQLite parent directory does not exist")
        self._closed = False
        try:
            self._connection = sqlite3.connect(path)
            # The standalone table has no parent key; the API-owned compatible table
            # does, so every adapter connection must enforce it when present.
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(self._CREATE)
            self._connection.commit()
            self._validate_schema()
        except sqlite3.Error as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise ReflectionStorageError(
                "SQLite repository could not be opened"
            ) from exc

    def __enter__(self) -> SQLiteReflectionRepository:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def put(self, reflection: RunReflection) -> None:
        self._require_open()
        checked = _validate_reflection(reflection)
        payload = checked.model_dump_json()
        if len(payload.encode()) > _MAX_SERIALIZED_BYTES:
            raise ReflectionStorageError("serialized reflection exceeds its limit")
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO run_reflections (tenant_id, session_id, run_id, payload)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (tenant_id, session_id, run_id)
                    DO UPDATE SET payload = excluded.payload
                    """,
                    (
                        checked.tenant_id,
                        checked.session_id,
                        checked.run_id,
                        payload,
                    ),
                )
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite reflection write failed") from exc

    def get(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, run_id: OpaqueId
    ) -> RunReflection | None:
        self._require_open()
        key = _key(tenant_id, session_id, run_id)
        try:
            row = self._connection.execute(
                """
                SELECT tenant_id, session_id, run_id, payload FROM run_reflections
                WHERE tenant_id = ? AND session_id = ? AND run_id = ?
                """,
                key,
            ).fetchone()
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite reflection read failed") from exc
        return _decode_row(row) if row is not None else None

    def list_session(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, limit: int = 100
    ) -> tuple[RunReflection, ...]:
        self._require_open()
        checked_tenant = _scope_id(tenant_id)
        checked_session = _scope_id(session_id)
        checked_limit = _limit(limit)
        try:
            rows = self._connection.execute(
                """
                SELECT tenant_id, session_id, run_id, payload FROM run_reflections
                WHERE tenant_id = ? AND session_id = ?
                ORDER BY run_id LIMIT ?
                """,
                (checked_tenant, checked_session, checked_limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite reflection list failed") from exc
        return tuple(_decode_row(row) for row in rows)

    def delete_run(
        self, *, tenant_id: OpaqueId, session_id: OpaqueId, run_id: OpaqueId
    ) -> bool:
        self._require_open()
        try:
            with self._connection:
                cursor = self._connection.execute(
                    """
                    DELETE FROM run_reflections
                    WHERE tenant_id = ? AND session_id = ? AND run_id = ?
                    """,
                    _key(tenant_id, session_id, run_id),
                )
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite run deletion failed") from exc
        return cursor.rowcount == 1

    def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int:
        self._require_open()
        try:
            with self._connection:
                cursor = self._connection.execute(
                    "DELETE FROM run_reflections WHERE tenant_id = ? AND session_id = ?",
                    (_scope_id(tenant_id), _scope_id(session_id)),
                )
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite session deletion failed") from exc
        return cursor.rowcount

    def delete_tenant(self, *, tenant_id: OpaqueId) -> int:
        self._require_open()
        try:
            with self._connection:
                cursor = self._connection.execute(
                    "DELETE FROM run_reflections WHERE tenant_id = ?",
                    (_scope_id(tenant_id),),
                )
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite tenant deletion failed") from exc
        return cursor.rowcount

    def _validate_schema(self) -> None:
        rows = self._connection.execute("PRAGMA table_info(run_reflections)").fetchall()
        schema = tuple((row[1], row[2], row[3], row[5]) for row in rows)
        if schema != self._EXPECTED_SCHEMA:
            self._connection.close()
            self._closed = True
            raise ReflectionStorageError("SQLite reflection schema is incompatible")

    def _require_open(self) -> None:
        if self._closed:
            raise RepositoryClosedError("SQLite reflection repository is closed")


def _validate_run_result(result: RunResult) -> RunResult:
    try:
        if (
            type(result) is not RunResult
            or type(result.snapshot) is not RunSnapshot
            or type(result.usage) is not RunUsage
            or type(result.events) is not tuple
            or len(result.events) > 16
            or any(type(event) is not PublicEvent for event in result.events)
            or type(result.snapshot.hits) is not tuple
            or len(result.snapshot.hits) > _MAX_RUN_HITS
            or any(type(hit) is not SearchHit for hit in result.snapshot.hits)
            or type(result.snapshot.evidence) is not tuple
            or len(result.snapshot.evidence) > 24
            or any(
                type(record) is not ExtractedEvidence
                for record in result.snapshot.evidence
            )
        ):
            raise ValueError("run result containers are invalid")
        answer = result.snapshot.answer
        if answer is not None and (
            type(answer) is not ScopedAnswer
            or type(answer.citations) is not tuple
            or len(answer.citations) > 16
            or any(type(citation) is not Citation for citation in answer.citations)
        ):
            raise ValueError("answer containers are invalid")
        payload = result.model_dump(mode="python", warnings="error")
        checked = RunResult.model_validate(payload, strict=True)
        if checked != result:
            raise ValueError("run result changed during validation")
        ReflectionUsage.model_validate(
            checked.usage.model_dump(mode="python", warnings="error"), strict=True
        )
        return checked
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise ReflectionInputError(
            "run result failed strict observable validation"
        ) from None


def _completion_evidence(snapshot: RunSnapshot) -> tuple[CompletionEvidence, ...]:
    if snapshot.terminal_state is not TerminalState.COMPLETED:
        return ()
    assert snapshot.answer is not None
    evidence = {record.evidence_id: record for record in snapshot.evidence}
    references: list[CompletionEvidence] = []
    for citation in snapshot.answer.citations:
        record = evidence.get(citation.evidence_id)
        if record is None or str(record.source_url) != str(citation.source_url):
            raise ReflectionInputError("completed citation provenance is invalid")
        if not _safe_public_url(str(citation.source_url)):
            raise ReflectionInputError("completion evidence URL is not safe to retain")
        references.append(
            CompletionEvidence(
                evidence_id=citation.evidence_id,
                source_url=citation.source_url,
            )
        )
    return tuple(references)


def _safe_public_url(raw_url: str) -> bool:
    try:
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or contains_sensitive_memory_hostname(parsed.hostname)
            or parsed.port not in {None, 80, 443}
            or parsed.hostname.casefold() in {"localhost", "localhost.localdomain"}
            or not SitePolicy().evaluate(parsed.hostname).allowed
        ):
            return False
        try:
            address = ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                return False
        decoded_path = _fully_decode_url_component(parsed.path, plus=False)
        decoded_query = _fully_decode_url_component(parsed.query, plus=True)
        query = parse_qsl(decoded_query, keep_blank_values=True)
        return (
            not parsed.fragment
            and not contains_sensitive_memory_text(decoded_path)
            and not contains_sensitive_memory_text(decoded_query)
            and not any(
                _sensitive_query_name(name) or contains_sensitive_memory_text(value)
                for name, value in query
            )
        )
    except (TypeError, ValueError):
        return False


def _fully_decode_url_component(value: str, *, plus: bool) -> str:
    """Expose nested percent encoding before deciding whether a URL is retainable."""

    decode = unquote_plus if plus else unquote
    decoded = value
    # Every effective pass shortens an encoded component, so the original length is
    # a strict work bound even for attacker-controlled recursive encoding.
    try:
        for _ in range(len(value) + 1):
            candidate = decode(decoded, errors="strict")
            if candidate == decoded:
                return decoded
            decoded = candidate
    except UnicodeDecodeError:
        raise ValueError("URL encoding is not safe to retain") from None
    raise ValueError("URL encoding exceeds the retention bound")


def _validate_reflection(reflection: RunReflection) -> RunReflection:
    try:
        if (
            type(reflection) is not RunReflection
            or type(reflection.actions) is not tuple
            or type(reflection.failures) is not tuple
            or any(type(item) is not ObservedFailure for item in reflection.failures)
            or type(reflection.recovery_steps) is not tuple
            or type(reflection.completion_evidence) is not tuple
            or any(
                type(item) is not CompletionEvidence
                for item in reflection.completion_evidence
            )
            or type(reflection.unresolved_items) is not tuple
            or type(reflection.usage) is not ReflectionUsage
        ):
            raise ValueError("reflection containers are invalid")
        payload = reflection.model_dump(mode="python", warnings="error")
        checked = RunReflection.model_validate(payload, strict=True)
        if checked != reflection or any(
            not _safe_public_url(str(item.source_url))
            for item in checked.completion_evidence
        ):
            raise ValueError("reflection failed safe validation")
        return checked
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise ReflectionInputError("reflection failed strict validation") from None


def _validate_stored_reflection(
    key: tuple[str, str, str], reflection: RunReflection
) -> RunReflection:
    checked = _validate_reflection(reflection)
    if (checked.tenant_id, checked.session_id, checked.run_id) != key:
        raise ReflectionInputError("stored reflection scope does not match its key")
    return checked


def _sensitive_query_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    return normalized in _SENSITIVE_QUERY_NAMES or normalized.endswith(
        _SENSITIVE_QUERY_SUFFIXES
    )


def _scope_id(value: object) -> str:
    try:
        return _ID_ADAPTER.validate_python(value, strict=True)
    except (TypeError, ValidationError, ValueError):
        raise ReflectionInputError("repository scope id is invalid") from None


def _key(tenant_id: object, session_id: object, run_id: object) -> tuple[str, str, str]:
    return (_scope_id(tenant_id), _scope_id(session_id), _scope_id(run_id))


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ReflectionInputError("repository list limit must be between 1 and 100")
    return value


def _decode_row(row: object) -> RunReflection:
    try:
        if (
            type(row) is not tuple
            or len(row) != 4
            or any(type(value) is not str for value in row)
            or len(row[3].encode()) > _MAX_SERIALIZED_BYTES
        ):
            raise ValueError("stored row has an invalid shape")
        reflection = _validate_reflection(
            RunReflection.model_validate_json(row[3], strict=True)
        )
        if (reflection.tenant_id, reflection.session_id, reflection.run_id) != row[:3]:
            raise ValueError("stored reflection scope does not match its row")
        return reflection
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise ReflectionStorageError("stored reflection failed validation") from None
