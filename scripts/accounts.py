"Owner-invoked account creation; credentials are written only to the private data dir."

import argparse
import asyncio
import json
import secrets
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from insightpilot.auth.service import create_account
from insightpilot.config import Settings
from insightpilot.runtime import new_event_loop
from insightpilot.types import Role
from scripts.private_files import replace_private, restrict_existing_private


def private_credential_path(settings):
    managed_private = (settings.workspace_root / ".data").resolve()
    directory = settings.data_dir.resolve()
    if not directory.is_relative_to(managed_private):
        raise ValueError("private_credential_directory")
    return directory / "accounts.json"


async def main(args):
    settings = Settings.from_environment(args.workspace_root)
    destination = private_credential_path(settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # This path is fixed by validated managed config, never a CLI export target.
    password = secrets.token_urlsafe(24)
    async with await psycopg.AsyncConnection.connect(
        settings.postgres_state_dsn.get_secret_value(), autocommit=True, row_factory=dict_row
    ) as conn:
        user_id = await create_account(
            conn, args.tenant_id, args.email, args.name, args.role, password
        )
    if destination.exists():
        restrict_existing_private(destination)
    existing = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else []
    existing.append(
        {
            "tenant_id": str(args.tenant_id),
            "user_id": str(user_id),
            "email": args.email,
            "role": args.role,
            "password": password,
        }
    )
    replace_private(destination, json.dumps(existing, ensure_ascii=False, indent=2).encode())
    print("Account created. Credentials saved in the private managed data directory.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=[r.value for r in Role], required=True)
    with asyncio.Runner(loop_factory=new_event_loop) as runner:
        runner.run(main(parser.parse_args()))
