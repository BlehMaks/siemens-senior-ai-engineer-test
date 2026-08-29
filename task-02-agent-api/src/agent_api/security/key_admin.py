"""API-key administration for local SQLite and shared Firestore state."""

from __future__ import annotations

import argparse
import asyncio
import os
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
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(plaintext)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise SystemExit("could not write the protected output file") from exc


if __name__ == "__main__":
    raise SystemExit(main())
