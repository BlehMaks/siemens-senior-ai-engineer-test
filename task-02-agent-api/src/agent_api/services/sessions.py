"""Tenant-scoped session lifecycle and seek pagination."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import TypeAdapter, ValidationError

from search_agent.contracts import OpaqueId

from ..schemas import PageCursor, SessionLabel
from ..storage import SessionRecord, SQLiteSessionRepository, StorageConflictError

_OPAQUE_ID = TypeAdapter(OpaqueId)
_PAGE_CURSOR = TypeAdapter(PageCursor)
_CREATE_ATTEMPTS = 4


class SessionNotFound(LookupError):
    """The session is absent from the authenticated tenant boundary."""


class InvalidRequest(ValueError):
    """The client supplied a semantically invalid request value."""


class InvalidCursor(InvalidRequest):
    """The client supplied an invalid opaque page cursor."""


class SessionUnavailable(RuntimeError):
    """A session identifier could not be allocated within the retry bound."""


class SessionService:
    def __init__(
        self,
        repository: SQLiteSessionRepository,
        *,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._id_factory = _new_session_id if id_factory is None else id_factory

    async def create(
        self, *, tenant_id: OpaqueId, label: SessionLabel | None
    ) -> SessionRecord:
        now = self._clock()
        for _ in range(_CREATE_ATTEMPTS):
            record = SessionRecord(
                tenant_id=tenant_id,
                session_id=self._id_factory(),
                label=label,
                created_at=now,
                updated_at=now,
            )
            try:
                if await self._repository.put(record):
                    return record
            except StorageConflictError:
                continue
        raise SessionUnavailable

    async def list(
        self,
        *,
        tenant_id: OpaqueId,
        limit: int,
        cursor: PageCursor | None,
    ) -> tuple[tuple[SessionRecord, ...], PageCursor | None]:
        after = None if cursor is None else _decode_cursor(cursor)
        records = await self._repository.list(
            tenant_id=tenant_id,
            limit=limit + 1,
            after=after,
        )
        items = records[:limit]
        next_cursor = (
            _encode_cursor(items[-1]) if len(records) > limit and items else None
        )
        return items, next_cursor

    async def get(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> SessionRecord:
        record = await self._repository.get(tenant_id=tenant_id, session_id=session_id)
        if record is None:
            raise SessionNotFound
        return record

    async def delete(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> None:
        await self.get(tenant_id=tenant_id, session_id=session_id)
        # A concurrent delete after this ownership check has the same terminal state.
        await self._repository.delete(tenant_id=tenant_id, session_id=session_id)

    async def delete_memory(self, *, tenant_id: OpaqueId, session_id: OpaqueId) -> int:
        await self.get(tenant_id=tenant_id, session_id=session_id)
        return await self._repository.delete_memory(
            tenant_id=tenant_id, session_id=session_id
        )

    def now(self) -> datetime:
        return self._clock()


def _new_session_id() -> str:
    encoded = base64.b32encode(secrets.token_bytes(16)).decode().lower().rstrip("=")
    return f"session-{encoded}"


def _encode_cursor(record: SessionRecord) -> PageCursor:
    payload = json.dumps(
        [record.created_at.isoformat(timespec="microseconds"), record.session_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return _PAGE_CURSOR.validate_python(encoded, strict=True)


def _decode_cursor(cursor: PageCursor) -> tuple[datetime, OpaqueId]:
    try:
        checked_cursor = _PAGE_CURSOR.validate_python(cursor, strict=True)
        payload = base64.urlsafe_b64decode(
            checked_cursor + "=" * (-len(checked_cursor) % 4)
        )
        if base64.urlsafe_b64encode(payload).decode().rstrip("=") != checked_cursor:
            raise ValueError("non-canonical cursor")
        values = json.loads(payload)
        if (
            type(values) is not list
            or len(values) != 2
            or any(type(value) is not str for value in values)
        ):
            raise ValueError("invalid cursor payload")
        created_at = datetime.fromisoformat(values[0])
        if (
            created_at.tzinfo is None
            or created_at.utcoffset() != timedelta(0)
            or created_at.isoformat(timespec="microseconds") != values[0]
        ):
            raise ValueError("invalid cursor timestamp")
        session_id = _OPAQUE_ID.validate_python(values[1], strict=True)
        return created_at, session_id
    except (
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
    ):
        raise InvalidCursor from None
