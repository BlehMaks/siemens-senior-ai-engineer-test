"""Local API-key bootstrap CLI."""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

from ..storage import (
    SQLiteKeyHashRepository,
    SQLiteTenantRepository,
    TenantRecord,
    migrate,
)
from .auth import ApiKeyManager, EnvPepperProvider

_DEFAULT_AUTHORIZATION_ENV = "AGENT_API_AUTHORIZATION"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-api-key-admin")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--pepper-env", default="AGENT_API_KEY_PEPPER")
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("create")
    create.add_argument("--tenant-id", required=True)
    create.add_argument("--scope", action="append", required=True)

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
    await migrate(args.db)
    now = datetime.now(UTC)
    manager = ApiKeyManager(
        SQLiteKeyHashRepository(args.db), EnvPepperProvider(args.pepper_env)
    )
    if args.command == "create":
        tenants = SQLiteTenantRepository(args.db)
        if await tenants.get(tenant_id=args.tenant_id) is None:
            await tenants.put(TenantRecord(tenant_id=args.tenant_id, created_at=now))
        generated = await manager.create(
            tenant_id=args.tenant_id, scopes=args.scope, now=now
        )
        return generated.plaintext
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


def _authorization(variable: str) -> str:
    value = os.environ.get(variable)
    if value is None:
        raise SystemExit("authorization environment variable is not set")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
