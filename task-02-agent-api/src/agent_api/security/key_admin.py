"""API-key administration for local SQLite and shared Firestore state."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
import os
import secrets
import stat
import sys
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from google.cloud.firestore_v1 import AsyncClient as FirestoreAsyncClient

import agent_api.storage.gcp as gcp_storage

from ..storage import (
    GoogleFirestoreDocumentStore,
    SQLiteKeyHashRepository,
    SQLiteTenantRepository,
    TenantRecord,
    migrate,
)
from .auth import ApiKeyManager, ApiKeyRepository, EnvPepperProvider
from .cloud_state import FirestoreApiKeyRepository

_DEFAULT_AUTHORIZATION_ENV = "AGENT_API_AUTHORIZATION"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-api-key-admin")
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument("--db", type=Path)
    backend.add_argument("--gcp-project")
    parser.add_argument("--firestore-database")
    parser.add_argument("--pepper-env", default="AGENT_API_KEY_PEPPER")
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("create")
    create.add_argument("--tenant-id", required=True)
    create.add_argument("--scope", action="append", required=True)
    create.add_argument("--output-file", type=Path)
    create.add_argument("--ttl-seconds", type=int)

    rotate = subcommands.add_parser("rotate")
    rotate.add_argument("--authorization-env", default=_DEFAULT_AUTHORIZATION_ENV)
    rotate.add_argument("--scope", action="append")

    revoke = subcommands.add_parser("revoke")
    revoke.add_argument("--authorization-env", default=_DEFAULT_AUTHORIZATION_ENV)

    args = parser.parse_args(argv)
    plaintext = asyncio.run(_run(args))
    if plaintext:
        print(plaintext)
    return 0


async def _run(args: argparse.Namespace) -> str | None:
    now = datetime.now(UTC)
    if args.db is not None:
        if args.firestore_database is not None:
            raise SystemExit("--firestore-database requires --gcp-project")
        await migrate(args.db)
        repository: ApiKeyRepository = SQLiteKeyHashRepository(args.db)
    else:
        repository = _cloud_key_repository(
            project=args.gcp_project,
            database=args.firestore_database or "(default)",
        )
    manager = ApiKeyManager(repository, EnvPepperProvider(args.pepper_env))
    if args.command == "create":
        if args.db is not None:
            tenants = SQLiteTenantRepository(args.db)
            if await tenants.get(tenant_id=args.tenant_id) is None:
                await tenants.put(
                    TenantRecord(tenant_id=args.tenant_id, created_at=now)
                )
        if args.ttl_seconds is not None and not 60 <= args.ttl_seconds <= 3600:
            raise SystemExit("--ttl-seconds must be between 60 and 3600")
        expires_at = (
            None
            if args.ttl_seconds is None
            else now + timedelta(seconds=args.ttl_seconds)
        )
        generated = await manager.create(
            tenant_id=args.tenant_id,
            scopes=args.scope,
            now=now,
            expires_at=expires_at,
            plaintext_sink=(
                None
                if args.output_file is None
                else lambda plaintext: _write_plaintext_file(
                    args.output_file, plaintext
                )
            ),
        )
        return None if args.output_file is not None else generated.plaintext
    if args.command == "rotate":
        generated = await manager.rotate(
            old_authorization=_authorization(args.authorization_env),
            scopes=args.scope,
            now=now,
        )
        return generated.plaintext
    if args.command == "revoke":
        await manager.revoke(
            authorization=_authorization(args.authorization_env), now=now
        )
        return None
    raise SystemExit(f"unsupported command: {args.command}")


def _cloud_key_repository(*, project: str, database: str) -> ApiKeyRepository:
    if not project or project.strip() != project:
        raise SystemExit("--gcp-project must be a non-empty clean value")
    if not database or database.strip() != database:
        raise SystemExit("--firestore-database must be a non-empty clean value")
    client = FirestoreAsyncClient(project=project, database=database)
    store = GoogleFirestoreDocumentStore(cast(gcp_storage._FirestoreClient, client))
    return FirestoreApiKeyRepository(store)


def _authorization(variable: str) -> str:
    value = os.environ.get(variable)
    if value is None:
        raise SystemExit("authorization environment variable is not set")
    return value


def _write_plaintext_file(path: Path, plaintext: str) -> None:
    if not path.is_absolute():
        raise SystemExit("--output-file must be an absolute path")
    directory_descriptor = -1
    descriptor = -1
    recovery_descriptor = -1
    created = False
    expected: tuple[int, int] | None = None
    try:
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        if not _same_directory(directory_descriptor, path.parent):
            raise OSError(errno.ESTALE, "output directory changed")
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        created = True
        opened_stat = os.fstat(descriptor)
        expected = (opened_stat.st_dev, opened_stat.st_ino)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as output:
            output.write(plaintext)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if not _same_directory(directory_descriptor, path.parent):
            raise OSError(errno.ESTALE, "output directory changed")
        os.fchmod(descriptor, 0o600)
        opened_stat = os.fstat(descriptor)
        path_stat = os.stat(
            path.name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or stat.S_IMODE(opened_stat.st_mode) != 0o600
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
            or stat.S_IMODE(path_stat.st_mode) != 0o600
        ):
            raise OSError(errno.ESTALE, "published output identity changed")
        os.fsync(directory_descriptor)
        if not _same_directory(directory_descriptor, path.parent):
            raise OSError(errno.ESTALE, "output directory changed")
        final_stat = os.stat(
            path.name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (final_stat.st_dev, final_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ) or stat.S_IMODE(final_stat.st_mode) != 0o600:
            raise OSError(errno.ESTALE, "published output identity changed")
        if not _same_directory(directory_descriptor, path.parent):
            raise OSError(errno.ESTALE, "output directory changed")
        recovery_descriptor = os.dup(descriptor)
        closing_descriptor = descriptor
        descriptor = -1
        os.close(closing_descriptor)
        closing_descriptor = recovery_descriptor
        recovery_descriptor = -1
        with suppress(OSError):
            os.close(closing_descriptor)
    except OSError as exc:
        owned_descriptor = descriptor if descriptor >= 0 else recovery_descriptor
        if owned_descriptor >= 0:
            try:
                current = os.fstat(owned_descriptor)
                current_identity = (current.st_dev, current.st_ino)
                if expected is None:
                    expected = current_identity
                if current_identity == expected:
                    os.ftruncate(owned_descriptor, 0)
                    os.fsync(owned_descriptor)
            except OSError:
                pass
        if created and expected is not None:
            _quarantine_owned_output(
                directory_descriptor,
                name=path.name,
                expected=expected,
            )
        if descriptor >= 0:
            closing_descriptor = descriptor
            descriptor = -1
            with suppress(OSError):
                os.close(closing_descriptor)
        if recovery_descriptor >= 0:
            closing_descriptor = recovery_descriptor
            recovery_descriptor = -1
            with suppress(OSError):
                os.close(closing_descriptor)
        raise SystemExit("could not write the protected output file") from exc
    finally:
        if directory_descriptor >= 0:
            with suppress(OSError):
                os.close(directory_descriptor)


def _same_directory(descriptor: int, path: Path) -> bool:
    try:
        opened = os.fstat(descriptor)
        current = path.stat()
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


def _quarantine_owned_output(
    descriptor: int,
    *,
    name: str,
    expected: tuple[int, int],
) -> None:
    if descriptor < 0:
        return
    try:
        current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except OSError:
        return
    if (current.st_dev, current.st_ino) != expected:
        return

    for _ in range(32):
        quarantine = f".api-key-cleanup-{secrets.token_hex(16)}"
        try:
            os.mkdir(quarantine, 0o700, dir_fd=descriptor)
        except FileExistsError:
            continue
        except OSError:
            return
        quarantined_name = f"{quarantine}/owned"
        try:
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            _create_empty_quarantine_marker(descriptor, quarantined_name)
            return
        if (current.st_dev, current.st_ino) != expected:
            _create_empty_quarantine_marker(descriptor, quarantined_name)
            return
        try:
            renamed = _rename_noreplace(
                descriptor,
                name,
                descriptor,
                quarantined_name,
            )
        except OSError:
            return
        if not renamed:
            continue
        try:
            quarantine_descriptor = os.open(
                quarantined_name,
                os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
        except OSError:
            return
        try:
            quarantined = os.fstat(quarantine_descriptor)
            if (quarantined.st_dev, quarantined.st_ino) == expected:
                os.ftruncate(quarantine_descriptor, 0)
                os.fsync(quarantine_descriptor)
            else:
                with suppress(OSError):
                    if _rename_noreplace(
                        descriptor,
                        quarantined_name,
                        descriptor,
                        name,
                    ):
                        _create_empty_quarantine_marker(descriptor, quarantined_name)
        except OSError:
            pass
        finally:
            closing_descriptor = quarantine_descriptor
            quarantine_descriptor = -1
            with suppress(OSError):
                os.close(closing_descriptor)
        with suppress(OSError):
            os.fsync(descriptor)
        return


def _create_empty_quarantine_marker(descriptor: int, name: str) -> None:
    marker_descriptor = -1
    try:
        marker_descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=descriptor,
        )
        os.fsync(marker_descriptor)
    except OSError:
        pass
    finally:
        if marker_descriptor >= 0:
            closing_descriptor = marker_descriptor
            marker_descriptor = -1
            with suppress(OSError):
                os.close(closing_descriptor)


def _rename_noreplace(
    source_descriptor: int,
    source: str,
    destination_descriptor: int,
    destination: str,
) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(libc, "renameatx_np", None)
        flag = 0x00000004
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        flag = 1
    else:
        rename = None
        flag = 0
    if rename is None:
        raise OSError(errno.ENOTSUP, "no no-replace rename primitive")

    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    result = rename(
        source_descriptor,
        os.fsencode(source),
        destination_descriptor,
        os.fsencode(destination),
        flag,
    )
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        return False
    raise OSError(error, os.strerror(error))


if __name__ == "__main__":
    raise SystemExit(main())
