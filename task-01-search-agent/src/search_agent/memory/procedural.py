"""Human-reviewed, versioned procedural memory stored only as bounded text data."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Annotated, Literal, Protocol, cast

from pydantic import (
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from ..contracts import OpaqueId, StrictModel
from .contracts import (
    ReflectionInputError,
    ReflectionStorageError,
    RepositoryClosedError,
    contains_memory_control_text,
    contains_sensitive_memory_text,
)
from .episodic import _limit, _scope_id

ProcedureTitle = Annotated[
    str, StringConstraints(min_length=3, max_length=80, strip_whitespace=True)
]
ProcedureStep = Annotated[
    str, StringConstraints(min_length=3, max_length=240, strip_whitespace=True)
]

_MAX_SERIALIZED_BYTES = 16 * 1024


class ProcedureAuthor(StrEnum):
    HUMAN = "human"
    DETERMINISTIC_TEST = "deterministic_test"


class ProcedureReviewState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class ProcedureReview(StrictModel):
    state: Literal[ProcedureReviewState.APPROVED, ProcedureReviewState.REJECTED]
    reviewer_id: OpaqueId
    reviewed_at: datetime

    @field_validator("reviewed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _timestamp(value)


class ProcedureVersion(StrictModel):
    schema_version: Literal[1] = 1
    tenant_id: OpaqueId
    procedure_id: OpaqueId
    version: int = Field(ge=1, le=10_000)
    origin_session_id: OpaqueId
    origin_run_id: OpaqueId
    title: ProcedureTitle
    steps: tuple[ProcedureStep, ...] = Field(min_length=1, max_length=8)
    proposed_at: datetime
    author: ProcedureAuthor
    state: ProcedureReviewState = ProcedureReviewState.PROPOSED
    review: ProcedureReview | None = None

    @field_validator("title", mode="before")
    @classmethod
    def validate_title(cls, value: object) -> object:
        return _memory_text(value)

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if type(value) is not tuple:
            raise ValueError("procedure steps must be an immutable tuple")
        return tuple(_memory_text(step) for step in value)

    @field_validator("proposed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _timestamp(value)

    @model_validator(mode="after")
    def validate_review(self) -> ProcedureVersion:
        if self.state is ProcedureReviewState.PROPOSED and self.review is not None:
            raise ValueError("proposed versions cannot have a review")
        if self.state is not ProcedureReviewState.PROPOSED and (
            self.review is None or self.review.state is not self.state
        ):
            raise ValueError("reviewed procedure state must match its review")
        if self.review is not None and self.review.reviewed_at < self.proposed_at:
            raise ValueError("procedure review cannot precede proposal")
        return self


class ProcedureVersionConflictError(ValueError):
    """A caller's expected latest or active version is stale."""


class ProcedureRepository(Protocol):
    def propose(
        self,
        procedure: ProcedureVersion,
        *,
        expected_latest_version: int | None,
    ) -> ProcedureVersion: ...

    def review(
        self,
        *,
        tenant_id: OpaqueId,
        procedure_id: OpaqueId,
        version: int,
        state: Literal[ProcedureReviewState.APPROVED, ProcedureReviewState.REJECTED],
        reviewer_id: OpaqueId,
        reviewed_at: datetime,
    ) -> ProcedureVersion: ...

    def activate(
        self,
        *,
        tenant_id: OpaqueId,
        procedure_id: OpaqueId,
        version: int,
        expected_active_version: int | None,
    ) -> ProcedureVersion: ...

    def get_version(
        self, *, tenant_id: OpaqueId, procedure_id: OpaqueId, version: int
    ) -> ProcedureVersion | None: ...

    def get_active(
        self, *, tenant_id: OpaqueId, procedure_id: OpaqueId
    ) -> ProcedureVersion | None: ...

    def list_versions(
        self, *, tenant_id: OpaqueId, procedure_id: OpaqueId, limit: int = 100
    ) -> tuple[ProcedureVersion, ...]: ...

    def list_active(
        self, *, tenant_id: OpaqueId, limit: int = 20
    ) -> tuple[ProcedureVersion, ...]: ...

    def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int: ...

    def delete_procedure(
        self, *, tenant_id: OpaqueId, procedure_id: OpaqueId
    ) -> int: ...

    def delete_tenant(self, *, tenant_id: OpaqueId) -> int: ...


class InMemoryProcedureRepository:
    def __init__(self) -> None:
        self._versions: dict[tuple[str, str, int], ProcedureVersion] = {}
        self._active: dict[tuple[str, str], int] = {}
        self._latest: dict[tuple[str, str], int] = {}
        self._generations: dict[tuple[str, str, int], int] = {}
        self._next_generation = 0
        self._lock = RLock()

    def propose(
        self,
        procedure: ProcedureVersion,
        *,
        expected_latest_version: int | None,
    ) -> ProcedureVersion:
        checked = _validate_procedure(procedure)
        if checked.state is not ProcedureReviewState.PROPOSED:
            raise ReflectionInputError("new procedure versions must start proposed")
        scope = _scope(checked.tenant_id, checked.procedure_id)
        with self._lock:
            latest = self._latest_version(scope)
        _require_expected(latest, expected_latest_version)
        if checked.version != (1 if latest is None else latest + 1):
            raise ProcedureVersionConflictError("procedure version is not sequential")
        with self._lock:
            if self._latest_version(scope) != latest:
                raise ProcedureVersionConflictError(
                    "procedure version expectation is stale"
                )
            key = (*scope, checked.version)
            self._next_generation += 1
            self._versions[key] = checked
            self._generations[key] = self._next_generation
            self._latest[scope] = checked.version
        return _copy(checked)

    def review(
        self,
        *,
        tenant_id: OpaqueId,
        procedure_id: OpaqueId,
        version: int,
        state: Literal[ProcedureReviewState.APPROVED, ProcedureReviewState.REJECTED],
        reviewer_id: OpaqueId,
        reviewed_at: datetime,
    ) -> ProcedureVersion:
        key = (*_scope(tenant_id, procedure_id), _version(version))
        with self._lock:
            current = self._required(key)
            generation = self._generation(key)
        updated = _reviewed(
            current,
            state=state,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
        )
        with self._lock:
            if (
                self._versions.get(key) != current
                or self._generation(key) != generation
            ):
                raise ProcedureVersionConflictError(
                    "procedure review raced another update"
                )
            self._versions[key] = updated
        return _copy(updated)

    def activate(
        self,
        *,
        tenant_id: OpaqueId,
        procedure_id: OpaqueId,
        version: int,
        expected_active_version: int | None,
    ) -> ProcedureVersion:
        scope = _scope(tenant_id, procedure_id)
        key = (*scope, _version(version))
        with self._lock:
            current_active = self._active.get(scope)
            stored = self._versions.get(key)
            generation = None if stored is None else self._generation(key)
        _require_expected(current_active, expected_active_version)
        if stored is None:
            raise ReflectionInputError("procedure version does not exist")
        selected = _validate_stored(key, stored)
        if selected.state is not ProcedureReviewState.APPROVED:
            raise ReflectionInputError("only approved procedure versions can be active")
        with self._lock:
            if self._active.get(scope) != current_active:
                raise ProcedureVersionConflictError(
                    "procedure version expectation is stale"
                )
            if self._versions.get(key) != stored or self._generation(key) != generation:
                raise ProcedureVersionConflictError(
                    "procedure version changed during activation"
                )
            self._active[scope] = selected.version
        return _copy(selected)

    def get_version(
        self, *, tenant_id: OpaqueId, procedure_id: OpaqueId, version: int
    ) -> ProcedureVersion | None:
        key = (*_scope(tenant_id, procedure_id), _version(version))
        with self._lock:
            stored = self._versions.get(key)
        return None if stored is None else _validate_stored(key, stored)

    def get_active(
        self, *, tenant_id: OpaqueId, procedure_id: OpaqueId
    ) -> ProcedureVersion | None:
        scope = _scope(tenant_id, procedure_id)
        with self._lock:
            version = self._active.get(scope)
            if version is not None and type(version) is not int:
                raise ReflectionStorageError("active procedure pointer is invalid")
            stored = None if version is None else self._versions.get((*scope, version))
        if version is None:
            return None
        if stored is None:
            raise ReflectionStorageError("active procedure pointer is dangling")
        selected = _validate_stored((*scope, version), stored)
        if selected.state is not ProcedureReviewState.APPROVED:
            raise ReflectionStorageError("active procedure is not approved")
        return selected

    def list_versions(
        self, *, tenant_id: OpaqueId, procedure_id: OpaqueId, limit: int = 100
    ) -> tuple[ProcedureVersion, ...]:
        scope = _scope(tenant_id, procedure_id)
        with self._lock:
            stored = tuple(self._versions.items())
        values = [
            _validate_stored(key, value) for key, value in stored if key[:2] == scope
        ]
        values.sort(key=lambda item: item.version, reverse=True)
        return tuple(values[: _limit(limit)])

    def list_active(
        self, *, tenant_id: OpaqueId, limit: int = 20
    ) -> tuple[ProcedureVersion, ...]:
        checked_tenant = _scope_id(tenant_id)
        with self._lock:
            values: list[ProcedureVersion] = []
            for scope, version in self._active.items():
                if scope[0] != checked_tenant:
                    continue
                if type(version) is not int:
                    raise ReflectionStorageError("active procedure pointer is invalid")
                values.append(self._version_for_activation((*scope, version)))
        values.sort(key=lambda item: item.procedure_id)
        return tuple(values[: _active_limit(limit)])

    def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        checked_session = _scope_id(session_id)
        with self._lock:
            keys = [
                key
                for key, item in self._versions.items()
                if item.tenant_id == checked_tenant
                and item.origin_session_id == checked_session
            ]
            selected = set(keys)
            for key in keys:
                del self._versions[key]
                self._generations.pop(key, None)
            for scope, version in tuple(self._active.items()):
                if (*scope, version) in selected:
                    del self._active[scope]
        return len(keys)

    def delete_procedure(self, *, tenant_id: OpaqueId, procedure_id: OpaqueId) -> int:
        scope = _scope(tenant_id, procedure_id)
        with self._lock:
            keys = [key for key in self._versions if key[:2] == scope]
            for key in keys:
                del self._versions[key]
                self._generations.pop(key, None)
            self._active.pop(scope, None)
        return len(keys)

    def delete_tenant(self, *, tenant_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        with self._lock:
            keys = [key for key in self._versions if key[0] == checked_tenant]
            for key in keys:
                del self._versions[key]
                self._generations.pop(key, None)
            scopes = [scope for scope in self._active if scope[0] == checked_tenant]
            for scope in scopes:
                del self._active[scope]
            latest_scopes = [
                scope for scope in self._latest if scope[0] == checked_tenant
            ]
            for scope in latest_scopes:
                del self._latest[scope]
        return len(keys)

    def _required(self, key: tuple[str, str, int]) -> ProcedureVersion:
        value = self._versions.get(key)
        if value is None:
            raise ReflectionInputError("procedure version does not exist")
        checked = _validate_stored(key, value)
        if checked.state is not ProcedureReviewState.PROPOSED:
            raise ReflectionInputError(
                "procedure version must be proposed before review"
            )
        return checked

    def _latest_version(self, scope: tuple[str, str]) -> int | None:
        history = [
            _validate_stored(key, value)
            for key, value in self._versions.items()
            if key[:2] == scope
        ]
        maximum = max((item.version for item in history), default=None)
        latest = self._latest.get(scope)
        if latest is None:
            if maximum is not None:
                raise ReflectionStorageError("procedure version head is missing")
            return None
        if type(latest) is not int or not 1 <= latest <= 10_000:
            raise ReflectionStorageError("procedure version head is invalid")
        if maximum is not None and latest < maximum:
            raise ReflectionStorageError("procedure version head is invalid")
        return latest

    def _generation(self, key: tuple[str, str, int]) -> int:
        generation = self._generations.get(key)
        if type(generation) is not int or generation <= 0:
            raise ReflectionStorageError("procedure record generation is invalid")
        return generation

    def _version_for_activation(self, key: tuple[str, str, int]) -> ProcedureVersion:
        value = self._versions.get(key)
        if value is None:
            raise ReflectionInputError("procedure version does not exist")
        checked = _validate_stored(key, value)
        if checked.state is not ProcedureReviewState.APPROVED:
            raise ReflectionInputError("only approved procedure versions can be active")
        return checked


class SQLiteProcedureRepository:
    _CREATE_VERSIONS = """
        CREATE TABLE IF NOT EXISTS procedure_versions (
            tenant_id TEXT NOT NULL,
            procedure_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            origin_session_id TEXT NOT NULL,
            origin_run_id TEXT NOT NULL,
            state TEXT NOT NULL,
            payload TEXT NOT NULL CHECK(length(payload) <= 16384),
            PRIMARY KEY (tenant_id, procedure_id, version)
        ) WITHOUT ROWID
    """
    _CREATE_ACTIVE = """
        CREATE TABLE IF NOT EXISTS active_procedures (
            tenant_id TEXT NOT NULL,
            procedure_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            PRIMARY KEY (tenant_id, procedure_id)
        ) WITHOUT ROWID
    """
    _CREATE_HEADS = """
        CREATE TABLE IF NOT EXISTS procedure_version_heads (
            tenant_id TEXT NOT NULL,
            procedure_id TEXT NOT NULL,
            latest_version INTEGER NOT NULL CHECK(
                latest_version BETWEEN 1 AND 10000
            ),
            PRIMARY KEY (tenant_id, procedure_id)
        ) WITHOUT ROWID
    """
    _EXPECTED_VERSIONS = (
        ("tenant_id", "TEXT", 1, 1),
        ("procedure_id", "TEXT", 1, 2),
        ("version", "INTEGER", 1, 3),
        ("origin_session_id", "TEXT", 1, 0),
        ("origin_run_id", "TEXT", 1, 0),
        ("state", "TEXT", 1, 0),
        ("payload", "TEXT", 1, 0),
    )
    _EXPECTED_ACTIVE = (
        ("tenant_id", "TEXT", 1, 1),
        ("procedure_id", "TEXT", 1, 2),
        ("version", "INTEGER", 1, 0),
    )
    _EXPECTED_HEADS = (
        ("tenant_id", "TEXT", 1, 1),
        ("procedure_id", "TEXT", 1, 2),
        ("latest_version", "INTEGER", 1, 0),
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
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 10000")
            self._connection.execute(self._CREATE_VERSIONS)
            self._connection.execute(self._CREATE_ACTIVE)
            self._connection.execute(self._CREATE_HEADS)
            self._connection.execute(
                "INSERT INTO procedure_version_heads "
                "(tenant_id, procedure_id, latest_version) "
                "SELECT tenant_id, procedure_id, MAX(version) "
                "FROM procedure_versions GROUP BY tenant_id, procedure_id "
                "ON CONFLICT (tenant_id, procedure_id) DO UPDATE SET "
                "latest_version = MAX(latest_version, excluded.latest_version)"
            )
            self._connection.commit()
            self._validate_schema()
        except sqlite3.Error as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise ReflectionStorageError(
                "SQLite procedure repository could not be opened"
            ) from exc

    def __enter__(self) -> SQLiteProcedureRepository:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def propose(
        self,
        procedure: ProcedureVersion,
        *,
        expected_latest_version: int | None,
    ) -> ProcedureVersion:
        self._require_open()
        checked = _validate_procedure(procedure)
        if checked.state is not ProcedureReviewState.PROPOSED:
            raise ReflectionInputError("new procedure versions must start proposed")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            scope = (checked.tenant_id, checked.procedure_id)
            self._validate_scope_metadata(*scope)
            latest = self._latest_version(scope)
            _require_expected(latest, expected_latest_version)
            if checked.version != (1 if latest is None else latest + 1):
                raise ProcedureVersionConflictError(
                    "procedure version is not sequential"
                )
            self._connection.execute(
                "INSERT INTO procedure_versions "
                "(tenant_id, procedure_id, version, origin_session_id, "
                "origin_run_id, state, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                _row_values(checked),
            )
            if latest is None:
                self._connection.execute(
                    "INSERT INTO procedure_version_heads "
                    "(tenant_id, procedure_id, latest_version) VALUES (?, ?, ?)",
                    (*scope, checked.version),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE procedure_version_heads SET latest_version = ? "
                    "WHERE tenant_id = ? AND procedure_id = ? "
                    "AND latest_version = ?",
                    (checked.version, *scope, latest),
                )
                if cursor.rowcount != 1:
                    raise ProcedureVersionConflictError(
                        "procedure version expectation is stale"
                    )
            self._connection.commit()
        except (ProcedureVersionConflictError, ReflectionInputError):
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            raise ReflectionStorageError("SQLite procedure proposal failed") from exc
        except BaseException:
            self._rollback()
            raise
        return _copy(checked)

    def review(
        self,
        *,
        tenant_id: OpaqueId,
        procedure_id: OpaqueId,
        version: int,
        state: Literal[ProcedureReviewState.APPROVED, ProcedureReviewState.REJECTED],
        reviewer_id: OpaqueId,
        reviewed_at: datetime,
    ) -> ProcedureVersion:
        self._require_open()
        key = (*_scope(tenant_id, procedure_id), _version(version))
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._validate_scope_metadata(key[0], key[1])
            self._latest_version(key[:2])
            current = self._required_row(key)
            if current.state is not ProcedureReviewState.PROPOSED:
                raise ReflectionInputError(
                    "procedure version must be proposed before review"
                )
            updated = _reviewed(
                current,
                state=state,
                reviewer_id=reviewer_id,
                reviewed_at=reviewed_at,
            )
            cursor = self._connection.execute(
                "UPDATE procedure_versions SET state = ?, payload = ? "
                "WHERE tenant_id = ? AND procedure_id = ? AND version = ? "
                "AND state = ?",
                (
                    updated.state,
                    updated.model_dump_json(),
                    *key,
                    ProcedureReviewState.PROPOSED,
                ),
            )
            if cursor.rowcount != 1:
                raise ProcedureVersionConflictError(
                    "procedure review raced another update"
                )
            self._connection.commit()
        except (ProcedureVersionConflictError, ReflectionInputError):
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            raise ReflectionStorageError("SQLite procedure review failed") from exc
        except BaseException:
            self._rollback()
            raise
        return _copy(updated)

    def activate(
        self,
        *,
        tenant_id: OpaqueId,
        procedure_id: OpaqueId,
        version: int,
        expected_active_version: int | None,
    ) -> ProcedureVersion:
        self._require_open()
        scope = _scope(tenant_id, procedure_id)
        key = (*scope, _version(version))
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._validate_scope_metadata(*scope)
            self._latest_version(scope)
            current_row = self._connection.execute(
                "SELECT version FROM active_procedures "
                "WHERE tenant_id = ? AND procedure_id = ?",
                scope,
            ).fetchone()
            current = None if current_row is None else current_row[0]
            _require_expected(current, expected_active_version)
            selected = self._required_row(key)
            if selected.state is not ProcedureReviewState.APPROVED:
                raise ReflectionInputError(
                    "only approved procedure versions can be active"
                )
            self._connection.execute(
                "INSERT INTO active_procedures (tenant_id, procedure_id, version) "
                "VALUES (?, ?, ?) ON CONFLICT (tenant_id, procedure_id) "
                "DO UPDATE SET version = excluded.version",
                key,
            )
            self._connection.commit()
        except (ProcedureVersionConflictError, ReflectionInputError):
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            raise ReflectionStorageError("SQLite procedure activation failed") from exc
        except BaseException:
            self._rollback()
            raise
        return _copy(selected)

    def get_version(
        self, *, tenant_id: OpaqueId, procedure_id: OpaqueId, version: int
    ) -> ProcedureVersion | None:
        self._require_open()
        scope = _scope(tenant_id, procedure_id)
        self._validate_scope_metadata(*scope)
        self._latest_version(scope)
        key = (*scope, _version(version))
        row = self._fetch_row(key)
        return None if row is None else _decode_row(row)

    def get_active(
        self, *, tenant_id: OpaqueId, procedure_id: OpaqueId
    ) -> ProcedureVersion | None:
        self._require_open()
        scope = _scope(tenant_id, procedure_id)
        self._validate_scope_metadata(*scope)
        self._latest_version(scope)
        try:
            pointer = self._connection.execute(
                "SELECT version FROM active_procedures "
                "WHERE tenant_id = ? AND procedure_id = ?",
                scope,
            ).fetchone()
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite active procedure read failed") from exc
        if pointer is None:
            return None
        if (
            type(pointer) is not tuple
            or len(pointer) != 1
            or type(pointer[0]) is not int
        ):
            raise ReflectionStorageError("active procedure pointer is invalid")
        row = self._fetch_row((*scope, pointer[0]))
        if row is None:
            raise ReflectionStorageError("active procedure pointer is dangling")
        selected = _decode_row(row)
        if selected.state is not ProcedureReviewState.APPROVED:
            raise ReflectionStorageError("active procedure is not approved")
        return selected

    def list_versions(
        self, *, tenant_id: OpaqueId, procedure_id: OpaqueId, limit: int = 100
    ) -> tuple[ProcedureVersion, ...]:
        self._require_open()
        scope = _scope(tenant_id, procedure_id)
        self._validate_scope_metadata(*scope)
        self._latest_version(scope)
        try:
            rows = self._connection.execute(
                "SELECT tenant_id, procedure_id, version, origin_session_id, "
                "origin_run_id, state, payload FROM procedure_versions "
                "WHERE tenant_id = ? AND procedure_id = ? "
                "ORDER BY version DESC LIMIT ?",
                (*scope, _limit(limit)),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite procedure list failed") from exc
        return tuple(_decode_row(row) for row in rows)

    def list_active(
        self, *, tenant_id: OpaqueId, limit: int = 20
    ) -> tuple[ProcedureVersion, ...]:
        self._require_open()
        checked_tenant = _scope_id(tenant_id)
        self._validate_scope_metadata(checked_tenant, None)
        try:
            pointers = self._connection.execute(
                "SELECT tenant_id, procedure_id, version FROM active_procedures "
                "WHERE tenant_id = ? ORDER BY procedure_id LIMIT ?",
                (checked_tenant, _active_limit(limit)),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite active procedure list failed") from exc
        values_list: list[ProcedureVersion] = []
        for pointer in pointers:
            if (
                type(pointer) is not tuple
                or len(pointer) != 3
                or any(type(value) is not str for value in pointer[:2])
                or type(pointer[2]) is not int
            ):
                raise ReflectionStorageError("active procedure pointer is invalid")
            self._latest_version((pointer[0], pointer[1]))
            row = self._fetch_row(pointer)
            if row is None:
                raise ReflectionStorageError("active procedure pointer is dangling")
            values_list.append(_decode_row(row))
        values = tuple(values_list)
        if any(item.state is not ProcedureReviewState.APPROVED for item in values):
            raise ReflectionStorageError("active procedure is not approved")
        return values

    def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int:
        self._require_open()
        scope = (_scope_id(tenant_id), _scope_id(session_id))
        return self._delete(
            "tenant_id = ? AND origin_session_id = ?",
            scope,
        )

    def delete_procedure(self, *, tenant_id: OpaqueId, procedure_id: OpaqueId) -> int:
        self._require_open()
        scope = _scope(tenant_id, procedure_id)
        return self._delete("tenant_id = ? AND procedure_id = ?", scope)

    def delete_tenant(self, *, tenant_id: OpaqueId) -> int:
        self._require_open()
        return self._delete("tenant_id = ?", (_scope_id(tenant_id),), delete_heads=True)

    def _delete(
        self,
        predicate: str,
        parameters: tuple[str, ...],
        *,
        delete_heads: bool = False,
    ) -> int:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            selected_rows = self._connection.execute(
                "SELECT tenant_id, procedure_id, version, origin_session_id, "
                "origin_run_id, state, payload FROM procedure_versions "
                f"WHERE {predicate} ORDER BY tenant_id, procedure_id, version",
                parameters,
            ).fetchall()
            selected_versions = tuple(_decode_row(row) for row in selected_rows)
            scopes = {(item.tenant_id, item.procedure_id) for item in selected_versions}
            for scope in scopes:
                self._latest_version(scope)
            selected = {
                (item.tenant_id, item.procedure_id, item.version)
                for item in selected_versions
            }
            pointers = self._connection.execute(
                "SELECT tenant_id, procedure_id, version FROM active_procedures"
            ).fetchall()
            for pointer in pointers:
                if tuple(pointer) in selected:
                    self._connection.execute(
                        "DELETE FROM active_procedures WHERE tenant_id = ? "
                        "AND procedure_id = ?",
                        pointer[:2],
                    )
            cursor = self._connection.execute(
                f"DELETE FROM procedure_versions WHERE {predicate}", parameters
            )
            for scope in sorted(scopes):
                remaining = self._connection.execute(
                    "SELECT 1 FROM procedure_versions WHERE tenant_id = ? "
                    "AND procedure_id = ? LIMIT 1",
                    scope,
                ).fetchone()
                if remaining is None:
                    self._connection.execute(
                        "DELETE FROM active_procedures WHERE tenant_id = ? "
                        "AND procedure_id = ?",
                        scope,
                    )
            if delete_heads:
                self._connection.execute(
                    f"DELETE FROM procedure_version_heads WHERE {predicate}",
                    parameters,
                )
            self._connection.commit()
            return cursor.rowcount
        except sqlite3.Error as exc:
            self._rollback()
            raise ReflectionStorageError("SQLite procedure deletion failed") from exc
        except BaseException:
            self._rollback()
            raise

    def _fetch_row(self, key: tuple[str, str, int]) -> tuple[object, ...] | None:
        try:
            row = self._connection.execute(
                "SELECT tenant_id, procedure_id, version, origin_session_id, "
                "origin_run_id, state, payload FROM procedure_versions "
                "WHERE tenant_id = ? AND procedure_id = ? AND version = ?",
                key,
            ).fetchone()
            return None if row is None else cast(tuple[object, ...], row)
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite procedure read failed") from exc

    def _required_row(self, key: tuple[str, str, int]) -> ProcedureVersion:
        row = self._fetch_row(key)
        if row is None:
            raise ReflectionInputError("procedure version does not exist")
        return _decode_row(row)

    def _validate_schema(self) -> None:
        version_rows = self._connection.execute(
            'PRAGMA table_info("procedure_versions")'
        ).fetchall()
        active_rows = self._connection.execute(
            'PRAGMA table_info("active_procedures")'
        ).fetchall()
        head_rows = self._connection.execute(
            'PRAGMA table_info("procedure_version_heads")'
        ).fetchall()
        versions = tuple((row[1], row[2], row[3], row[5]) for row in version_rows)
        active = tuple((row[1], row[2], row[3], row[5]) for row in active_rows)
        heads = tuple((row[1], row[2], row[3], row[5]) for row in head_rows)
        if (
            versions != self._EXPECTED_VERSIONS
            or active != self._EXPECTED_ACTIVE
            or heads != self._EXPECTED_HEADS
        ):
            self.close()
            raise ReflectionStorageError("SQLite procedure schema is incompatible")

    def _validate_scope_metadata(
        self, tenant_id: str, procedure_id: str | None
    ) -> None:
        predicate = (
            "(tenant_id = ? OR (json_valid(payload) AND "
            "json_extract(payload, '$.tenant_id') = ?))"
        )
        parameters: list[str] = [tenant_id, tenant_id]
        if procedure_id is not None:
            predicate += (
                " AND (procedure_id = ? OR (json_valid(payload) AND "
                "json_extract(payload, '$.procedure_id') = ?))"
            )
            parameters.extend((procedure_id, procedure_id))
        try:
            rows = self._connection.execute(
                "SELECT tenant_id, procedure_id, version, origin_session_id, "
                "origin_run_id, state, payload FROM procedure_versions WHERE "
                f"{predicate} ORDER BY tenant_id, procedure_id, version LIMIT -1",
                tuple(parameters),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ReflectionStorageError(
                "SQLite procedure metadata validation failed"
            ) from exc
        for row in rows:
            _decode_row(row)

    def _latest_version(self, scope: tuple[str, str]) -> int | None:
        row = self._connection.execute(
            "SELECT latest_version FROM procedure_version_heads "
            "WHERE tenant_id = ? AND procedure_id = ?",
            scope,
        ).fetchone()
        maximum_row = self._connection.execute(
            "SELECT MAX(version) FROM procedure_versions "
            "WHERE tenant_id = ? AND procedure_id = ?",
            scope,
        ).fetchone()
        maximum = None if maximum_row is None else maximum_row[0]
        if row is None:
            if maximum is not None:
                raise ReflectionStorageError("procedure version head is missing")
            return None
        if type(row) is not tuple or len(row) != 1 or type(row[0]) is not int:
            raise ReflectionStorageError("procedure version head is invalid")
        latest = row[0]
        if not 1 <= latest <= 10_000:
            raise ReflectionStorageError("procedure version head is invalid")
        if maximum is not None and (type(maximum) is not int or latest < maximum):
            raise ReflectionStorageError("procedure version head is invalid")
        return latest

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.rollback()

    def _require_open(self) -> None:
        if self._closed:
            raise RepositoryClosedError("SQLite procedure repository is closed")


def _memory_text(value: object) -> str:
    if (
        type(value) is not str
        or contains_sensitive_memory_text(value)
        or contains_memory_control_text(value)
    ):
        raise ValueError("procedure text contains sensitive or controlling material")
    return " ".join(value.split())


def _timestamp(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("memory timestamps must be timezone-aware datetimes")
    return value.astimezone(UTC)


def _version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 10_000:
        raise ReflectionInputError("procedure version is invalid")
    return value


def _active_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 20:
        raise ReflectionInputError("active procedure limit is invalid")
    return value


def _scope(tenant_id: object, procedure_id: object) -> tuple[str, str]:
    return (_scope_id(tenant_id), _scope_id(procedure_id))


def _require_expected(current: int | None, expected: object) -> None:
    if expected is not None:
        expected = _version(expected)
    if current != expected:
        raise ProcedureVersionConflictError("procedure version expectation is stale")


def _validate_procedure(procedure: ProcedureVersion) -> ProcedureVersion:
    try:
        if (
            type(procedure) is not ProcedureVersion
            or type(procedure.steps) is not tuple
            or any(type(step) is not str for step in procedure.steps)
            or (
                procedure.review is not None
                and type(procedure.review) is not ProcedureReview
            )
        ):
            raise ValueError("procedure containers are invalid")
        checked = ProcedureVersion.model_validate(
            procedure.model_dump(mode="python", warnings="error"), strict=True
        )
        if (
            checked != procedure
            or len(checked.model_dump_json().encode()) > _MAX_SERIALIZED_BYTES
        ):
            raise ValueError("procedure failed safe validation")
        return checked
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise ReflectionInputError("procedure failed strict validation") from None


def _validate_stored(
    key: tuple[str, str, int], value: ProcedureVersion
) -> ProcedureVersion:
    checked = _validate_procedure(value)
    if (checked.tenant_id, checked.procedure_id, checked.version) != key:
        raise ReflectionStorageError("stored procedure scope does not match its key")
    return _copy(checked)


def _copy(value: ProcedureVersion) -> ProcedureVersion:
    return ProcedureVersion.model_validate_json(value.model_dump_json(), strict=True)


def _reviewed(
    procedure: ProcedureVersion,
    *,
    state: object,
    reviewer_id: object,
    reviewed_at: datetime,
) -> ProcedureVersion:
    if (
        state is not ProcedureReviewState.APPROVED
        and state is not ProcedureReviewState.REJECTED
    ):
        raise ReflectionInputError("procedure review decision is invalid")
    try:
        review = ProcedureReview(
            state=state,
            reviewer_id=_scope_id(reviewer_id),
            reviewed_at=_timestamp(reviewed_at),
        )
        return ProcedureVersion.model_validate(
            {
                **procedure.model_dump(mode="python"),
                "state": state,
                "review": review,
            },
            strict=True,
        )
    except (TypeError, ValidationError, ValueError):
        raise ReflectionInputError("procedure review is invalid") from None


def _row_values(
    value: ProcedureVersion,
) -> tuple[str, str, int, str, str, str, str]:
    return (
        value.tenant_id,
        value.procedure_id,
        value.version,
        value.origin_session_id,
        value.origin_run_id,
        value.state,
        value.model_dump_json(),
    )


def _decode_row(row: object) -> ProcedureVersion:
    try:
        if (
            type(row) is not tuple
            or len(row) != 7
            or any(type(value) is not str for value in (*row[:2], *row[3:]))
            or type(row[2]) is not int
            or len(row[6].encode()) > _MAX_SERIALIZED_BYTES
        ):
            raise ValueError("stored procedure row has an invalid shape")
        procedure = _validate_procedure(
            ProcedureVersion.model_validate_json(row[6], strict=True)
        )
        if (
            procedure.tenant_id,
            procedure.procedure_id,
            procedure.version,
            procedure.origin_session_id,
            procedure.origin_run_id,
            procedure.state,
        ) != row[:6]:
            raise ValueError("stored procedure metadata does not match its row")
        return procedure
    except (
        AttributeError,
        TypeError,
        ValidationError,
        ValueError,
        ReflectionInputError,
    ) as exc:
        raise ReflectionStorageError("stored procedure is invalid") from exc
