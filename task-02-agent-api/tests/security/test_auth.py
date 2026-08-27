from __future__ import annotations

import base64
import logging
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_api.security.auth import (
    ApiKeyAuthError,
    ApiKeyManager,
    EnvPepperProvider,
    api_key_digest,
    parse_authorization_header,
)
from agent_api.storage import (
    SQLiteKeyHashRepository,
    SQLiteTenantRepository,
    TenantRecord,
)

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class FixedPepper:
    def __init__(self, pepper: bytes = b"p" * 32) -> None:
        self._pepper = pepper

    def pepper(self) -> bytes:
        return self._pepper


class DateTimeSubclass(datetime):
    pass


async def _tenant(path: Path) -> None:
    await SQLiteTenantRepository(path).put(
        TenantRecord(tenant_id="tenant-one", created_at=NOW)
    )


def _wrong_secret(plaintext: str) -> str:
    prefix, secret_text = plaintext.rsplit(".", 1)
    first = "a" if secret_text[0] != "a" else "b"
    return f"{prefix}.{first}{secret_text[1:]}"


def _noncanonical_secret(plaintext: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    prefix, secret_text = plaintext.rsplit(".", 1)
    final_index = alphabet.index(secret_text[-1])
    assert final_index % 4 == 0
    return f"{prefix}.{secret_text[:-1]}{alphabet[final_index + 1]}"


@pytest.mark.asyncio
async def test_create_verify_format_and_digest_only_at_rest(
    migrated_path: Path,
) -> None:
    await _tenant(migrated_path)
    manager = ApiKeyManager(SQLiteKeyHashRepository(migrated_path), FixedPepper())

    generated = await manager.create(
        tenant_id="tenant-one",
        scopes=("runs:read", "runs:write"),
        now=NOW,
    )
    credentials = parse_authorization_header(f"Bearer {generated.plaintext}")

    assert generated.plaintext.startswith("sai.v1.tenant-one.key-")
    assert len(credentials.secret) == 32
    assert "=" not in generated.plaintext
    assert generated.plaintext not in repr(generated)
    assert "secret=" not in repr(credentials)
    assert "key_hash=" not in repr(generated.record)
    assert credentials.tenant_id not in repr(credentials)
    assert credentials.key_id not in repr(credentials)
    assert credentials.tenant_id not in repr(generated.record)
    assert credentials.key_id not in repr(generated.record)
    authenticated = await manager.authenticate(
        authorization=f"Bearer {generated.plaintext}",
        required_scope="runs:read",
        now=NOW,
    )
    assert authenticated.tenant_id == "tenant-one"
    assert authenticated.scopes == ("runs:read", "runs:write")
    assert authenticated.tenant_id not in repr(authenticated)
    assert authenticated.key_id not in repr(authenticated)

    raw = migrated_path.read_bytes()
    assert generated.plaintext.encode() not in raw
    assert credentials.secret not in raw
    stored = await SQLiteKeyHashRepository(migrated_path).get(
        tenant_id="tenant-one", key_id=credentials.key_id
    )
    assert stored is not None
    assert stored.key_hash == api_key_digest(credentials, b"p" * 32)
    assert stored.key_hash in raw

    with pytest.raises(ApiKeyAuthError):
        parse_authorization_header(
            f"Bearer {_noncanonical_secret(generated.plaintext)}"
        )


@pytest.mark.asyncio
async def test_wrong_secret_tenant_key_revoked_expired_and_scope_are_rejected(
    migrated_path: Path,
) -> None:
    await _tenant(migrated_path)
    manager = ApiKeyManager(SQLiteKeyHashRepository(migrated_path), FixedPepper())
    generated = await manager.create(
        tenant_id="tenant-one",
        scopes=("runs:read",),
        now=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    plaintext = generated.plaintext
    credentials = parse_authorization_header(f"Bearer {plaintext}")
    invalid_key = _wrong_secret(plaintext)
    wrong_tenant = plaintext.replace("tenant-one", "tenant-two", 1)
    wrong_key = plaintext.replace(
        credentials.key_id, "key-aaaaaaaaaaaaaaaaaaaaaaaaaa", 1
    )

    for bad in (invalid_key, wrong_tenant, wrong_key):
        with pytest.raises(ApiKeyAuthError) as excinfo:
            await manager.authenticate(
                authorization=f"Bearer {bad}", required_scope="runs:read", now=NOW
            )
        assert excinfo.value.status_code == 401

    with pytest.raises(ApiKeyAuthError) as excinfo:
        await manager.authenticate(
            authorization=f"Bearer {plaintext}",
            required_scope="sessions:delete",
            now=NOW,
        )
    assert excinfo.value.status_code == 403

    with pytest.raises(ApiKeyAuthError) as excinfo:
        await manager.authenticate(
            authorization=f"Bearer {plaintext}",
            required_scope="runs:read",
            now=NOW + timedelta(hours=2),
        )
    assert excinfo.value.status_code == 401

    assert await SQLiteKeyHashRepository(migrated_path).revoke(
        tenant_id="tenant-one",
        key_id=credentials.key_id,
        at=NOW + timedelta(minutes=5),
    )
    with pytest.raises(ApiKeyAuthError) as excinfo:
        await manager.authenticate(
            authorization=f"Bearer {plaintext}",
            required_scope="runs:read",
            now=NOW + timedelta(minutes=6),
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_rotate_is_atomic_and_revoke_requires_matching_secret(
    migrated_path: Path,
) -> None:
    await _tenant(migrated_path)
    manager = ApiKeyManager(SQLiteKeyHashRepository(migrated_path), FixedPepper())
    old = await manager.create(tenant_id="tenant-one", scopes=("runs:read",), now=NOW)
    bad_old = _wrong_secret(old.plaintext)
    with pytest.raises(ApiKeyAuthError):
        await manager.rotate(
            old_authorization=f"Bearer {bad_old}",
            scopes=("runs:write",),
            now=NOW + timedelta(seconds=1),
        )

    new = await manager.rotate(
        old_authorization=f"Bearer {old.plaintext}",
        scopes=("runs:write",),
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ApiKeyAuthError):
        await manager.authenticate(
            authorization=f"Bearer {old.plaintext}",
            required_scope="runs:read",
            now=NOW + timedelta(seconds=2),
        )
    assert (
        await manager.authenticate(
            authorization=f"Bearer {new.plaintext}",
            required_scope="runs:write",
            now=NOW + timedelta(seconds=2),
        )
    ).scopes == ("runs:write",)

    bad_new = _wrong_secret(new.plaintext)
    with pytest.raises(ApiKeyAuthError):
        await manager.revoke(
            authorization=f"Bearer {bad_new}", now=NOW + timedelta(seconds=3)
        )
    assert await manager.revoke(
        authorization=f"Bearer {new.plaintext}", now=NOW + timedelta(seconds=3)
    )
    assert not await manager.revoke(
        authorization=f"Bearer {new.plaintext}", now=NOW + timedelta(seconds=4)
    )


@pytest.mark.asyncio
async def test_rotation_preserves_expiry_unless_replaced(migrated_path: Path) -> None:
    await _tenant(migrated_path)
    manager = ApiKeyManager(SQLiteKeyHashRepository(migrated_path), FixedPepper())
    expiry = NOW + timedelta(minutes=10)
    old = await manager.create(
        tenant_id="tenant-one",
        scopes=("runs:read",),
        now=NOW,
        expires_at=expiry,
    )

    new = await manager.rotate(
        old_authorization=f"Bearer {old.plaintext}",
        scopes=None,
        now=NOW + timedelta(seconds=1),
    )

    assert new.record.expires_at == expiry
    with pytest.raises(ApiKeyAuthError):
        await manager.authenticate(
            authorization=f"Bearer {new.plaintext}",
            required_scope="runs:read",
            now=expiry,
        )


@pytest.mark.asyncio
async def test_migrated_old_key_row_has_empty_scopes_and_cannot_authorize(
    migrated_path: Path,
) -> None:
    await _tenant(migrated_path)
    generated = await ApiKeyManager(
        SQLiteKeyHashRepository(migrated_path), FixedPepper()
    ).create(tenant_id="tenant-one", scopes=("runs:read",), now=NOW)
    credentials = parse_authorization_header(f"Bearer {generated.plaintext}")
    with sqlite3.connect(migrated_path) as connection:
        connection.execute(
            "UPDATE api_key_hashes SET scopes = '[]' "
            "WHERE tenant_id = ? AND key_id = ?",
            ("tenant-one", credentials.key_id),
        )

    with pytest.raises(ApiKeyAuthError) as excinfo:
        await ApiKeyManager(
            SQLiteKeyHashRepository(migrated_path), FixedPepper()
        ).authenticate(
            authorization=f"Bearer {generated.plaintext}",
            required_scope="runs:read",
            now=NOW,
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_compare_digest_runs_for_existing_and_missing_keys(
    migrated_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _tenant(migrated_path)
    manager = ApiKeyManager(SQLiteKeyHashRepository(migrated_path), FixedPepper())
    generated = await manager.create(
        tenant_id="tenant-one", scopes=("runs:read",), now=NOW
    )
    calls: list[tuple[int, int]] = []

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((len(left), len(right)))
        return left == right

    monkeypatch.setattr("agent_api.security.auth.hmac.compare_digest", spy)
    assert await manager.authenticate(
        authorization=f"Bearer {generated.plaintext}",
        required_scope="runs:read",
        now=NOW,
    )
    missing = generated.plaintext.replace("tenant-one", "tenant-two", 1)
    with pytest.raises(ApiKeyAuthError):
        await manager.authenticate(
            authorization=f"Bearer {missing}", required_scope="runs:read", now=NOW
        )

    with sqlite3.connect(migrated_path) as connection:
        connection.execute(
            "UPDATE api_key_hashes SET key_hash = ? WHERE tenant_id = ? AND key_id = ?",
            (
                b"h" * 64,
                "tenant-one",
                parse_authorization_header(f"Bearer {generated.plaintext}").key_id,
            ),
        )
    with pytest.raises(ApiKeyAuthError):
        await manager.authenticate(
            authorization=f"Bearer {generated.plaintext}",
            required_scope="runs:read",
            now=NOW,
        )

    assert calls == [(32, 32), (32, 32), (32, 32)]


@pytest.mark.asyncio
async def test_broken_pepper_datetime_subclasses_and_logs_are_safe(
    migrated_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _tenant(migrated_path)
    monkeypatch.setenv(
        "AGENT_API_KEY_PEPPER", base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    )
    manager = ApiKeyManager(SQLiteKeyHashRepository(migrated_path), EnvPepperProvider())
    generated = await manager.create(
        tenant_id="tenant-one", scopes=("runs:read",), now=NOW
    )
    plaintext = generated.plaintext

    monkeypatch.setenv("AGENT_API_KEY_PEPPER", "short")
    with pytest.raises(ApiKeyAuthError):
        await manager.authenticate(
            authorization=f"Bearer {plaintext}", required_scope="runs:read", now=NOW
        )
    monkeypatch.setenv(
        "AGENT_API_KEY_PEPPER", base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    )
    with pytest.raises(ValueError, match="exact datetime"):
        await manager.authenticate(
            authorization=f"Bearer {plaintext}",
            required_scope="runs:read",
            now=DateTimeSubclass(2026, 8, 27, 10, 0, tzinfo=UTC),
        )

    with caplog.at_level(logging.INFO), suppress(ApiKeyAuthError):
        await manager.authenticate(
            authorization=f"Bearer {plaintext[:-1]}x",
            required_scope="runs:read",
            now=NOW,
        )
    captured = "\n".join(caplog.messages)
    assert plaintext not in captured
    assert "p" * 32 not in captured
    stored = await SQLiteKeyHashRepository(migrated_path).get(
        tenant_id="tenant-one",
        key_id=parse_authorization_header(f"Bearer {plaintext}").key_id,
    )
    assert stored is not None
    assert stored.key_hash.hex() not in captured


@given(st.text(min_size=0, max_size=260))
@settings(max_examples=150)
def test_authorization_parser_accepts_only_its_canonical_encoding(header: str) -> None:
    try:
        credentials = parse_authorization_header(header)
    except ApiKeyAuthError as error:
        assert error.__cause__ is None
        return
    encoded_key_material = (
        base64.urlsafe_b64encode(credentials.secret).decode().rstrip("=")
    )
    assert header == (
        "Bearer sai.v1."
        f"{credentials.tenant_id}.{credentials.key_id}.{encoded_key_material}"
    )
