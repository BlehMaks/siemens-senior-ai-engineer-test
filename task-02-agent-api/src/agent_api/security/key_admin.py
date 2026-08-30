"""API-key administration for local SQLite and shared Firestore state."""

from __future__ import annotations

import argparse
import asyncio
import errno
import os
import secrets
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
    temporary_name: str | None = None
    published = False
    try:
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        if not _same_directory(directory_descriptor, path.parent):
            raise OSError(errno.ESTALE, "output directory changed")
        for _ in range(32):
            candidate = f".api-key-{secrets.token_hex(8)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if descriptor < 0 or temporary_name is None:
            raise OSError(errno.EEXIST, "could not reserve a temporary output file")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(plaintext)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if not _same_directory(directory_descriptor, path.parent):
            raise OSError(errno.ESTALE, "output directory changed")
        os.link(
            temporary_name,
            path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        published = True
        if not _same_directory(directory_descriptor, path.parent):
            raise OSError(errno.ESTALE, "output directory changed")
        temporary_stat = os.stat(
            temporary_name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        published_stat = os.stat(
            path.name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (temporary_stat.st_dev, temporary_stat.st_ino) != (
            published_stat.st_dev,
            published_stat.st_ino,
        ) or published_stat.st_mode & 0o777 != 0o600:
            raise OSError(errno.ESTALE, "published output identity changed")
        os.fsync(directory_descriptor)
    except OSError as exc:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if published and temporary_name is not None:
            _unlink_same_inode(
                directory_descriptor,
                source=temporary_name,
                destination=path.name,
            )
        if temporary_name is not None and directory_descriptor >= 0:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=directory_descriptor)
        raise SystemExit("could not write the protected output file") from exc
    else:
        assert temporary_name is not None
        with suppress(OSError):
            os.unlink(temporary_name, dir_fd=directory_descriptor)
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


def _unlink_same_inode(descriptor: int, *, source: str, destination: str) -> None:
    if descriptor < 0:
        return
    try:
        source_stat = os.stat(source, dir_fd=descriptor, follow_symlinks=False)
        destination_stat = os.stat(
            destination, dir_fd=descriptor, follow_symlinks=False
        )
        if (source_stat.st_dev, source_stat.st_ino) == (
            destination_stat.st_dev,
            destination_stat.st_ino,
        ):
            os.unlink(destination, dir_fd=descriptor)
    except OSError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
