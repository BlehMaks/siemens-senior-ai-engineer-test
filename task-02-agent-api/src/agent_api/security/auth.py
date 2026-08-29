"""Versioned API-key creation and verification without plaintext persistence."""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from search_agent.contracts import OpaqueId, StrictModel

from ..storage import ApiKeyHashRecord, ApiKeyScope

_OPAQUE_ID = TypeAdapter(OpaqueId)
_SCOPE = TypeAdapter(ApiKeyScope)
_DOMAIN = b"agent-api-key-hash:v1\x00"
_DUMMY_DIGEST = b"\x00" * 32
_HEADER_LIMIT = 220
_SECRET_BYTES = 32
_SECRET_TEXT_LENGTH = 43
_TOKEN_PREFIX = "sai.v1"
_DEFAULT_PEPPER_ENV = "AGENT_API_KEY_PEPPER"


class PepperProvider(Protocol):
    def pepper(self) -> bytes: ...


class ApiKeyRepository(Protocol):
    """Persistence contract required by the key lifecycle boundary."""

    async def put(self, record: ApiKeyHashRecord) -> bool: ...

    async def get(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId
    ) -> ApiKeyHashRecord | None: ...

    async def revoke(
        self, *, tenant_id: OpaqueId, key_id: OpaqueId, at: datetime
    ) -> bool: ...

    async def rotate(
        self,
        *,
        old_tenant_id: OpaqueId,
        old_key_id: OpaqueId,
        new_record: ApiKeyHashRecord,
        at: datetime,
    ) -> bool: ...


class EnvPepperProvider:
    """Loads a local base64url pepper; deployment Secret Manager stays out of P03."""

    def __init__(self, variable: str = _DEFAULT_PEPPER_ENV) -> None:
        self._variable = variable

    def pepper(self) -> bytes:
        value = os.environ.get(self._variable)
        if value is None:
            raise ApiKeyAuthError.unauthenticated()
        if (
            type(value) is not str
            or value.strip() != value
            or any(char in value for char in "\r\n\t,")
        ):
            raise ApiKeyAuthError.unauthenticated()
        try:
            decoded = _b64decode(value)
        except ValueError:
            raise ApiKeyAuthError.unauthenticated() from None
        if len(decoded) < 32:
            raise ApiKeyAuthError.unauthenticated()
        return decoded


class ApiKeyAuthError(RuntimeError):
    """Safe auth failure; message never identifies a tenant, key, or secret."""

    def __init__(
        self,
        status_code: int,
        code: str = "unauthenticated",
        *,
        tenant_id: OpaqueId | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.tenant_id = tenant_id
        super().__init__("Authentication failed" if status_code == 401 else "Forbidden")

    @classmethod
    def unauthenticated(cls) -> ApiKeyAuthError:
        return cls(401, "unauthenticated")

    @classmethod
    def forbidden(cls, *, tenant_id: OpaqueId) -> ApiKeyAuthError:
        return cls(403, "forbidden", tenant_id=tenant_id)


class ApiKeyCredentials(StrictModel):
    tenant_id: OpaqueId = Field(repr=False)
    key_id: OpaqueId = Field(repr=False)
    secret: bytes = Field(
        min_length=_SECRET_BYTES, max_length=_SECRET_BYTES, repr=False
    )

    @field_validator("secret", mode="before")
    @classmethod
    def require_secret_bytes(cls, value: object) -> object:
        if type(value) is not bytes:
            raise ValueError("key secret must be exact bytes")
        return value


class AuthenticatedApiKey(StrictModel):
    tenant_id: OpaqueId = Field(repr=False)
    key_id: OpaqueId = Field(repr=False)
    scopes: tuple[ApiKeyScope, ...] = Field(repr=False)
    expires_at: datetime | None = None


@dataclass(frozen=True)
class GeneratedApiKey:
    plaintext: str = field(repr=False)
    record: ApiKeyHashRecord = field(repr=False)


class ApiKeyManager:
    def __init__(
        self, repository: ApiKeyRepository, pepper_provider: PepperProvider
    ) -> None:
        self._repository = repository
        self._pepper_provider = pepper_provider

    async def create(
        self,
        *,
        tenant_id: OpaqueId,
        scopes: Sequence[str],
        now: datetime,
        expires_at: datetime | None = None,
        plaintext_sink: Callable[[str], None] | None = None,
    ) -> GeneratedApiKey:
        generated = generate_api_key(
            tenant_id=tenant_id,
            scopes=scopes,
            now=now,
            expires_at=expires_at,
            pepper=self._pepper_provider.pepper(),
        )
        if plaintext_sink is not None:
            plaintext_sink(generated.plaintext)
        await self._repository.put(generated.record)
        return generated

    async def rotate(
        self,
        *,
        old_authorization: str,
        scopes: Sequence[str] | None,
        now: datetime,
        expires_at: datetime | None = None,
    ) -> GeneratedApiKey:
        old = parse_authorization_header(old_authorization)
        record = await self._matching_record(credentials=old, now=now)
        new_scopes = record.scopes if scopes is None else _validate_scopes(scopes)
        generated = generate_api_key(
            tenant_id=old.tenant_id,
            scopes=new_scopes,
            now=now,
            expires_at=record.expires_at if expires_at is None else expires_at,
            pepper=self._pepper_provider.pepper(),
            rotated_from_key_id=old.key_id,
        )
        if not await self._repository.rotate(
            old_tenant_id=old.tenant_id,
            old_key_id=old.key_id,
            new_record=generated.record,
            at=now,
        ):
            raise ApiKeyAuthError.unauthenticated()
        return generated

    async def revoke(self, *, authorization: str, now: datetime) -> bool:
        credentials = parse_authorization_header(authorization)
        await self._matching_record(
            credentials=credentials, now=now, require_active=False
        )
        return await self._repository.revoke(
            tenant_id=credentials.tenant_id, key_id=credentials.key_id, at=now
        )

    async def authenticate(
        self,
        *,
        authorization: str | None,
        required_scope: str,
        now: datetime,
    ) -> AuthenticatedApiKey:
        if authorization is None:
            raise ApiKeyAuthError.unauthenticated()
        credentials = parse_authorization_header(authorization)
        required = _SCOPE.validate_python(required_scope, strict=True)
        record = await self._matching_record(credentials=credentials, now=now)
        if required not in record.scopes:
            raise ApiKeyAuthError.forbidden(tenant_id=record.tenant_id)
        return AuthenticatedApiKey(
            tenant_id=record.tenant_id,
            key_id=record.key_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
        )

    async def _matching_record(
        self,
        *,
        credentials: ApiKeyCredentials,
        now: datetime,
        require_active: bool = True,
    ) -> ApiKeyHashRecord:
        pepper = self._pepper_provider.pepper()
        candidate = api_key_digest(credentials, pepper)
        record = await self._repository.get(
            tenant_id=credentials.tenant_id, key_id=credentials.key_id
        )
        stored_digest = (
            record.key_hash
            if record is not None and len(record.key_hash) == len(_DUMMY_DIGEST)
            else _DUMMY_DIGEST
        )
        matched = _compare_digest(stored_digest, candidate)
        if record is None or len(record.key_hash) != len(_DUMMY_DIGEST) or not matched:
            raise ApiKeyAuthError.unauthenticated()
        if require_active and record.status_at(now) != "active":
            raise ApiKeyAuthError.unauthenticated()
        return record


def generate_api_key(
    *,
    tenant_id: OpaqueId,
    scopes: Sequence[str],
    now: datetime,
    expires_at: datetime | None,
    pepper: bytes,
    rotated_from_key_id: OpaqueId | None = None,
) -> GeneratedApiKey:
    checked_now = _utc(now)
    key_id = "key-" + base64.b32encode(secrets.token_bytes(16)).decode().lower().rstrip(
        "="
    )
    secret_bytes = secrets.token_bytes(_SECRET_BYTES)
    credentials = ApiKeyCredentials(
        tenant_id=_OPAQUE_ID.validate_python(tenant_id, strict=True),
        key_id=key_id,
        secret=secret_bytes,
    )
    record = ApiKeyHashRecord(
        tenant_id=credentials.tenant_id,
        key_id=credentials.key_id,
        key_hash=api_key_digest(credentials, pepper),
        scopes=_validate_scopes(scopes),
        created_at=checked_now,
        expires_at=None if expires_at is None else _utc(expires_at),
        rotated_from_key_id=rotated_from_key_id,
    )
    return GeneratedApiKey(plaintext=_format_key(credentials), record=record)


def parse_authorization_header(header: str) -> ApiKeyCredentials:
    try:
        if type(header) is not str or len(header) > _HEADER_LIMIT:
            raise ValueError("bad header")
        if any(char in header for char in "\r\n\t,") or header.count(" ") != 1:
            raise ValueError("bad header")
        scheme, key_text = header.split(" ", 1)
        if scheme != "Bearer":
            raise ValueError("bad scheme")
        parts = key_text.split(".")
        if len(parts) != 5 or parts[0] != "sai" or parts[1] != "v1":
            raise ValueError("bad token")
        tenant_id = _OPAQUE_ID.validate_python(parts[2], strict=True)
        key_id = _OPAQUE_ID.validate_python(parts[3], strict=True)
        secret_text = parts[4]
        if len(secret_text) != _SECRET_TEXT_LENGTH or "=" in secret_text:
            raise ValueError("bad secret")
        material = _b64decode(secret_text)
        if len(material) != _SECRET_BYTES or _b64encode(material) != secret_text:
            raise ValueError("bad secret")
        return ApiKeyCredentials(tenant_id=tenant_id, key_id=key_id, secret=material)
    except (ValidationError, ValueError):
        raise ApiKeyAuthError.unauthenticated() from None


def api_key_digest(credentials: ApiKeyCredentials, pepper: bytes) -> bytes:
    if type(pepper) is not bytes or len(pepper) < 32:
        raise ApiKeyAuthError.unauthenticated()
    message = (
        _DOMAIN
        + credentials.tenant_id.encode()
        + b"\x00"
        + credentials.key_id.encode()
        + b"\x00"
        + credentials.secret
    )
    return hmac.digest(pepper, message, "sha256")


def _compare_digest(left: bytes, right: bytes) -> bool:
    return hmac.compare_digest(left, right)


def _format_key(credentials: ApiKeyCredentials) -> str:
    return ".".join(
        (
            _TOKEN_PREFIX,
            credentials.tenant_id,
            credentials.key_id,
            _b64encode(credentials.secret),
        )
    )


def _validate_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(scopes, tuple | list) or len(scopes) == 0 or len(scopes) > 64:
        raise ValueError("key must have between one and 64 scopes")
    return tuple(
        sorted({_SCOPE.validate_python(scope, strict=True) for scope in scopes})
    )


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise ValueError("timestamp must be an exact datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or any(
        char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for char in value
    ):
        raise ValueError("invalid base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64url") from exc
