"""Review-gated, citation-backed semantic facts."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import (
    AnyHttpUrl,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from ..contracts import EvidenceId, OpaqueId, StrictModel
from .contracts import (
    ReflectionInputError,
    ReflectionStorageError,
    RepositoryClosedError,
    contains_memory_control_text,
    contains_sensitive_memory_text,
)
from .episodic import _limit, _safe_public_url, _scope_id

FactClaim = Annotated[
    str, StringConstraints(min_length=3, max_length=400, strip_whitespace=True)
]

_MAX_SERIALIZED_BYTES = 16 * 1024


class FactAuthor(StrEnum):
    HUMAN = "human"
    DETERMINISTIC_TEST = "deterministic_test"


class FactReviewState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class FactReview(StrictModel):
    state: Literal[FactReviewState.APPROVED, FactReviewState.REJECTED]
    reviewer_id: OpaqueId
    reviewed_at: datetime

    @field_validator("reviewed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _timestamp(value)


class SemanticFact(StrictModel):
    schema_version: Literal[1] = 1
    tenant_id: OpaqueId
    fact_id: OpaqueId
    origin_session_id: OpaqueId
    origin_run_id: OpaqueId
    claim: FactClaim
    conflict_key: OpaqueId
    source_id: OpaqueId
    evidence_id: EvidenceId
    source_url: AnyHttpUrl
    proposed_at: datetime
    expires_at: datetime
    author: FactAuthor
    state: FactReviewState = FactReviewState.PROPOSED
    review: FactReview | None = None

    @field_validator("claim", mode="before")
    @classmethod
    def normalize_claim(cls, value: object) -> object:
        if (
            type(value) is not str
            or contains_sensitive_memory_text(value)
            or contains_memory_control_text(value)
        ):
            raise ValueError("fact claim contains sensitive material")
        return " ".join(value.split()).casefold()

    @field_validator("proposed_at", "expires_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _timestamp(value)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> SemanticFact:
        if self.expires_at <= self.proposed_at:
            raise ValueError("fact expiry must follow proposal time")
        if self.state is FactReviewState.PROPOSED and self.review is not None:
            raise ValueError("proposed facts cannot have a review")
        if self.state is not FactReviewState.PROPOSED and (
            self.review is None or self.review.state is not self.state
        ):
            raise ValueError("reviewed fact state must match its review")
        if self.review is not None and self.review.reviewed_at < self.proposed_at:
            raise ValueError("fact review cannot precede its proposal")
        return self


class FactConflictError(ValueError):
    """Approval would create two active claims for one conflict identity."""


class SemanticFactRepository(Protocol):
    def propose(self, fact: SemanticFact) -> SemanticFact: ...

    def review(
        self,
        *,
        tenant_id: OpaqueId,
        fact_id: OpaqueId,
        state: Literal[FactReviewState.APPROVED, FactReviewState.REJECTED],
        reviewer_id: OpaqueId,
        reviewed_at: datetime,
    ) -> SemanticFact: ...

    def reopen(self, *, tenant_id: OpaqueId, fact_id: OpaqueId) -> SemanticFact: ...

    def get(self, *, tenant_id: OpaqueId, fact_id: OpaqueId) -> SemanticFact | None: ...

    def list_proposed(
        self, *, tenant_id: OpaqueId, limit: int = 100
    ) -> tuple[SemanticFact, ...]: ...

    def list_active(
        self, *, tenant_id: OpaqueId, at: datetime, limit: int = 100
    ) -> tuple[SemanticFact, ...]: ...

    def delete_fact(self, *, tenant_id: OpaqueId, fact_id: OpaqueId) -> bool: ...

    def delete_source(self, *, tenant_id: OpaqueId, source_id: OpaqueId) -> int: ...

    def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int: ...

    def delete_tenant(self, *, tenant_id: OpaqueId) -> int: ...


class InMemorySemanticFactRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], SemanticFact] = {}

    def propose(self, fact: SemanticFact) -> SemanticFact:
        checked = _validate_fact(fact)
        if checked.state is not FactReviewState.PROPOSED:
            raise ReflectionInputError("new facts must start proposed")
        key = _key(checked.tenant_id, checked.fact_id)
        if key in self._items:
            raise ReflectionInputError("fact id already exists")
        self._items[key] = checked
        return _copy(checked)

    def review(
        self,
        *,
        tenant_id: OpaqueId,
        fact_id: OpaqueId,
        state: Literal[FactReviewState.APPROVED, FactReviewState.REJECTED],
        reviewer_id: OpaqueId,
        reviewed_at: datetime,
    ) -> SemanticFact:
        key = _key(tenant_id, fact_id)
        current = self._required(key)
        updated = _reviewed(
            current,
            state=state,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
        )
        if updated.state is FactReviewState.APPROVED:
            assert updated.review is not None
            _reject_conflict(
                updated, self._items.values(), at=updated.review.reviewed_at
            )
        self._items[key] = updated
        return _copy(updated)

    def reopen(self, *, tenant_id: OpaqueId, fact_id: OpaqueId) -> SemanticFact:
        key = _key(tenant_id, fact_id)
        current = self._items.get(key)
        if current is None:
            raise ReflectionInputError("fact does not exist")
        current = _validate_stored_fact(key, current)
        if current.state is FactReviewState.PROPOSED:
            raise ReflectionInputError("fact is already proposed")
        updated = SemanticFact.model_validate(
            {
                **current.model_dump(mode="python"),
                "state": FactReviewState.PROPOSED,
                "review": None,
            },
            strict=True,
        )
        self._items[key] = updated
        return _copy(updated)

    def get(self, *, tenant_id: OpaqueId, fact_id: OpaqueId) -> SemanticFact | None:
        key = _key(tenant_id, fact_id)
        value = self._items.get(key)
        return None if value is None else _validate_stored_fact(key, value)

    def list_proposed(
        self, *, tenant_id: OpaqueId, limit: int = 100
    ) -> tuple[SemanticFact, ...]:
        checked_tenant = _scope_id(tenant_id)
        tenant_facts = [
            _validate_stored_fact(key, fact)
            for key, fact in self._items.items()
            if key[0] == checked_tenant
        ]
        values = [
            fact for fact in tenant_facts if fact.state is FactReviewState.PROPOSED
        ]
        values.sort(key=lambda fact: fact.fact_id)
        return tuple(values[: _limit(limit)])

    def list_active(
        self, *, tenant_id: OpaqueId, at: datetime, limit: int = 100
    ) -> tuple[SemanticFact, ...]:
        checked_tenant = _scope_id(tenant_id)
        checked_at = _timestamp(at)
        tenant_facts = [
            _validate_stored_fact(key, fact)
            for key, fact in self._items.items()
            if key[0] == checked_tenant
        ]
        values = [
            fact
            for fact in tenant_facts
            if fact.state is FactReviewState.APPROVED and fact.expires_at > checked_at
        ]
        values.sort(key=lambda fact: (fact.conflict_key, fact.fact_id))
        return tuple(values[: _limit(limit)])

    def delete_fact(self, *, tenant_id: OpaqueId, fact_id: OpaqueId) -> bool:
        return self._items.pop(_key(tenant_id, fact_id), None) is not None

    def delete_source(self, *, tenant_id: OpaqueId, source_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        checked_source = _scope_id(source_id)
        keys = [
            key
            for key, fact in self._items.items()
            if key[0] == checked_tenant and fact.source_id == checked_source
        ]
        for key in keys:
            del self._items[key]
        return len(keys)

    def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int:
        checked_tenant = _scope_id(tenant_id)
        checked_session = _scope_id(session_id)
        keys = [
            key
            for key, fact in self._items.items()
            if key[0] == checked_tenant and fact.origin_session_id == checked_session
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

    def _required(self, key: tuple[str, str]) -> SemanticFact:
        value = self._items.get(key)
        if value is None:
            raise ReflectionInputError("fact does not exist")
        if value.state is not FactReviewState.PROPOSED:
            raise ReflectionInputError("fact must be proposed before review")
        return _validate_stored_fact(key, value)


class SQLiteSemanticFactRepository:
    _CREATE = """
        CREATE TABLE IF NOT EXISTS semantic_facts (
            tenant_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            origin_session_id TEXT NOT NULL,
            origin_run_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            conflict_key TEXT NOT NULL,
            state TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            payload TEXT NOT NULL CHECK(length(payload) <= 16384),
            PRIMARY KEY (tenant_id, fact_id)
        ) WITHOUT ROWID
    """
    _EXPECTED_SCHEMA = (
        ("tenant_id", "TEXT", 1, 1),
        ("fact_id", "TEXT", 1, 2),
        ("origin_session_id", "TEXT", 1, 0),
        ("origin_run_id", "TEXT", 1, 0),
        ("source_id", "TEXT", 1, 0),
        ("conflict_key", "TEXT", 1, 0),
        ("state", "TEXT", 1, 0),
        ("expires_at", "TEXT", 1, 0),
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
            self._connection = sqlite3.connect(path, check_same_thread=False)
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 10000")
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

    def __enter__(self) -> SQLiteSemanticFactRepository:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def propose(self, fact: SemanticFact) -> SemanticFact:
        self._require_open()
        checked = _validate_fact(fact)
        if checked.state is not FactReviewState.PROPOSED:
            raise ReflectionInputError("new facts must start proposed")
        values = _row_values(checked)
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO semantic_facts "
                    "(tenant_id, fact_id, origin_session_id, origin_run_id, "
                    "source_id, conflict_key, state, expires_at, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
        except sqlite3.IntegrityError as exc:
            raise ReflectionInputError("fact id already exists") from exc
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite semantic fact write failed") from exc
        return _copy(checked)

    def review(
        self,
        *,
        tenant_id: OpaqueId,
        fact_id: OpaqueId,
        state: Literal[FactReviewState.APPROVED, FactReviewState.REJECTED],
        reviewer_id: OpaqueId,
        reviewed_at: datetime,
    ) -> SemanticFact:
        self._require_open()
        key = _key(tenant_id, fact_id)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT tenant_id, fact_id, origin_session_id, origin_run_id, "
                "source_id, conflict_key, state, expires_at, payload "
                "FROM semantic_facts WHERE tenant_id = ? AND fact_id = ?",
                key,
            ).fetchone()
            if row is None:
                raise ReflectionInputError("fact does not exist")
            current = _decode_row(row)
            if current.state is not FactReviewState.PROPOSED:
                raise ReflectionInputError("fact must be proposed before review")
            updated = _reviewed(
                current,
                state=state,
                reviewer_id=reviewer_id,
                reviewed_at=reviewed_at,
            )
            if updated.state is FactReviewState.APPROVED:
                assert updated.review is not None
                conflicts = self._connection.execute(
                    "SELECT tenant_id, fact_id, origin_session_id, origin_run_id, "
                    "source_id, conflict_key, state, expires_at, payload "
                    "FROM semantic_facts WHERE tenant_id = ? AND conflict_key = ? "
                    "AND state = ? AND fact_id <> ? AND expires_at > ? LIMIT 1",
                    (
                        updated.tenant_id,
                        updated.conflict_key,
                        FactReviewState.APPROVED,
                        updated.fact_id,
                        _iso(updated.review.reviewed_at),
                    ),
                ).fetchall()
                _reject_conflict(
                    updated,
                    (_decode_row(item) for item in conflicts),
                    at=updated.review.reviewed_at,
                )
            cursor = self._connection.execute(
                "UPDATE semantic_facts SET state = ?, expires_at = ?, payload = ? "
                "WHERE tenant_id = ? AND fact_id = ? AND state = ?",
                (
                    updated.state,
                    _iso(updated.expires_at),
                    updated.model_dump_json(),
                    updated.tenant_id,
                    updated.fact_id,
                    FactReviewState.PROPOSED,
                ),
            )
            if cursor.rowcount != 1:
                raise ReflectionStorageError(
                    "semantic fact review raced another update"
                )
            self._connection.commit()
        except (FactConflictError, ReflectionInputError, ReflectionStorageError):
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise ReflectionStorageError("SQLite semantic fact review failed") from exc
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        return _copy(updated)

    def reopen(self, *, tenant_id: OpaqueId, fact_id: OpaqueId) -> SemanticFact:
        self._require_open()
        key = _key(tenant_id, fact_id)
        current = self.get(tenant_id=key[0], fact_id=key[1])
        if current is None:
            raise ReflectionInputError("fact does not exist")
        if current.state is FactReviewState.PROPOSED:
            raise ReflectionInputError("fact is already proposed")
        updated = SemanticFact.model_validate(
            {
                **current.model_dump(mode="python"),
                "state": FactReviewState.PROPOSED,
                "review": None,
            },
            strict=True,
        )
        try:
            with self._connection:
                cursor = self._connection.execute(
                    "UPDATE semantic_facts SET state = ?, payload = ? "
                    "WHERE tenant_id = ? AND fact_id = ? AND state = ?",
                    (
                        FactReviewState.PROPOSED,
                        updated.model_dump_json(),
                        key[0],
                        key[1],
                        current.state,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ReflectionStorageError(
                        "semantic fact reopen raced another update"
                    )
        except ReflectionStorageError:
            raise
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite semantic fact reopen failed") from exc
        return _copy(updated)

    def get(self, *, tenant_id: OpaqueId, fact_id: OpaqueId) -> SemanticFact | None:
        self._require_open()
        key = _key(tenant_id, fact_id)
        try:
            row = self._connection.execute(
                "SELECT tenant_id, fact_id, origin_session_id, origin_run_id, "
                "source_id, conflict_key, state, expires_at, payload "
                "FROM semantic_facts WHERE tenant_id = ? AND fact_id = ?",
                key,
            ).fetchone()
        except sqlite3.Error as exc:
            raise ReflectionStorageError("SQLite semantic fact read failed") from exc
        return None if row is None else _decode_row(row)

    def list_proposed(
        self, *, tenant_id: OpaqueId, limit: int = 100
    ) -> tuple[SemanticFact, ...]:
        checked_tenant = _scope_id(tenant_id)
        values = tuple(
            fact
            for fact in self._tenant_facts(checked_tenant)
            if fact.state is FactReviewState.PROPOSED
        )
        return values[: _limit(limit)]

    def list_active(
        self, *, tenant_id: OpaqueId, at: datetime, limit: int = 100
    ) -> tuple[SemanticFact, ...]:
        checked_tenant = _scope_id(tenant_id)
        checked_at = _timestamp(at)
        values = sorted(
            (
                fact
                for fact in self._tenant_facts(checked_tenant)
                if fact.state is FactReviewState.APPROVED
                and fact.expires_at > checked_at
            ),
            key=lambda fact: (fact.conflict_key, fact.fact_id),
        )
        return tuple(values[: _limit(limit)])

    def delete_fact(self, *, tenant_id: OpaqueId, fact_id: OpaqueId) -> bool:
        return (
            self._delete("tenant_id = ? AND fact_id = ?", _key(tenant_id, fact_id)) == 1
        )

    def delete_source(self, *, tenant_id: OpaqueId, source_id: OpaqueId) -> int:
        return self._delete(
            "tenant_id = ? AND source_id = ?",
            (_scope_id(tenant_id), _scope_id(source_id)),
        )

    def delete_session(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int:
        return self._delete(
            "tenant_id = ? AND origin_session_id = ?",
            (_scope_id(tenant_id), _scope_id(session_id)),
        )

    def delete_tenant(self, *, tenant_id: OpaqueId) -> int:
        return self._delete("tenant_id = ?", (_scope_id(tenant_id),))

    def _delete(self, predicate: str, parameters: tuple[str, ...]) -> int:
        self._require_open()
        try:
            with self._connection:
                cursor = self._connection.execute(
                    f"DELETE FROM semantic_facts WHERE {predicate}", parameters
                )
        except sqlite3.Error as exc:
            raise ReflectionStorageError(
                "SQLite semantic fact deletion failed"
            ) from exc
        return cursor.rowcount

    def _validate_schema(self) -> None:
        rows = self._connection.execute(
            'PRAGMA table_info("semantic_facts")'
        ).fetchall()
        schema = tuple((row[1], row[2], row[3], row[5]) for row in rows)
        if schema != self._EXPECTED_SCHEMA:
            self._connection.close()
            self._closed = True
            raise ReflectionStorageError("SQLite semantic fact schema is incompatible")

    def _tenant_facts(self, tenant_id: str) -> tuple[SemanticFact, ...]:
        self._require_open()
        try:
            rows = self._connection.execute(
                "SELECT tenant_id, fact_id, origin_session_id, origin_run_id, "
                "source_id, conflict_key, state, expires_at, payload FROM "
                "semantic_facts WHERE tenant_id = ? ORDER BY fact_id LIMIT -1",
                (tenant_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ReflectionStorageError(
                "SQLite semantic fact metadata validation failed"
            ) from exc
        return tuple(_decode_row(row) for row in rows)

    def _require_open(self) -> None:
        if self._closed:
            raise RepositoryClosedError("SQLite semantic fact repository is closed")


def _timestamp(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("memory timestamps must be timezone-aware datetimes")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _key(tenant_id: object, fact_id: object) -> tuple[str, str]:
    return (_scope_id(tenant_id), _scope_id(fact_id))


def _validate_fact(fact: SemanticFact) -> SemanticFact:
    try:
        if type(fact) is not SemanticFact or (
            fact.review is not None and type(fact.review) is not FactReview
        ):
            raise ValueError("fact containers are invalid")
        checked = SemanticFact.model_validate(
            fact.model_dump(mode="python", warnings="error"), strict=True
        )
        if checked != fact or not _safe_public_url(str(checked.source_url)):
            raise ValueError("fact failed safe validation")
        if len(checked.model_dump_json().encode()) > _MAX_SERIALIZED_BYTES:
            raise ValueError("fact exceeds storage limit")
        return checked
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise ReflectionInputError("fact failed strict validation") from None


def _validate_stored_fact(key: tuple[str, str], fact: SemanticFact) -> SemanticFact:
    checked = _validate_fact(fact)
    if (checked.tenant_id, checked.fact_id) != key:
        raise ReflectionStorageError("stored fact scope does not match its key")
    return _copy(checked)


def _copy(fact: SemanticFact) -> SemanticFact:
    return SemanticFact.model_validate_json(fact.model_dump_json(), strict=True)


def _reviewed(
    fact: SemanticFact,
    *,
    state: object,
    reviewer_id: object,
    reviewed_at: datetime,
) -> SemanticFact:
    if state is not FactReviewState.APPROVED and state is not FactReviewState.REJECTED:
        raise ReflectionInputError("fact review decision is invalid")
    try:
        review = FactReview(
            state=state,
            reviewer_id=_scope_id(reviewer_id),
            reviewed_at=_timestamp(reviewed_at),
        )
        if review.reviewed_at >= fact.expires_at:
            raise ValueError("expired facts cannot be reviewed")
        return SemanticFact.model_validate(
            {**fact.model_dump(mode="python"), "state": state, "review": review},
            strict=True,
        )
    except (TypeError, ValidationError, ValueError):
        raise ReflectionInputError("fact review is invalid") from None


def _reject_conflict(
    candidate: SemanticFact,
    facts: Iterable[SemanticFact],
    *,
    at: datetime,
) -> None:
    if candidate.review is None:
        raise ReflectionInputError("approved facts require review")
    for fact in facts:
        checked = _validate_fact(fact)
        if (
            checked.tenant_id == candidate.tenant_id
            and checked.fact_id != candidate.fact_id
            and checked.state is FactReviewState.APPROVED
            and checked.conflict_key == candidate.conflict_key
            and checked.expires_at > at
            and checked.claim != candidate.claim
        ):
            raise FactConflictError("an active conflicting claim is already approved")


def _row_values(
    fact: SemanticFact,
) -> tuple[str, str, str, str, str, str, str, str, str]:
    return (
        fact.tenant_id,
        fact.fact_id,
        fact.origin_session_id,
        fact.origin_run_id,
        fact.source_id,
        fact.conflict_key,
        fact.state,
        _iso(fact.expires_at),
        fact.model_dump_json(),
    )


def _decode_row(row: object) -> SemanticFact:
    try:
        if (
            type(row) is not tuple
            or len(row) != 9
            or any(type(value) is not str for value in row)
            or len(row[8].encode()) > _MAX_SERIALIZED_BYTES
        ):
            raise ValueError("stored row has an invalid shape")
        fact = _validate_fact(SemanticFact.model_validate_json(row[8], strict=True))
        if (
            fact.tenant_id,
            fact.fact_id,
            fact.origin_session_id,
            fact.origin_run_id,
            fact.source_id,
            fact.conflict_key,
            fact.state,
            _iso(fact.expires_at),
        ) != row[:8]:
            raise ValueError("stored fact metadata does not match its row")
        return fact
    except (
        AttributeError,
        TypeError,
        ValidationError,
        ValueError,
        ReflectionInputError,
    ) as exc:
        raise ReflectionStorageError("stored semantic fact is invalid") from exc
