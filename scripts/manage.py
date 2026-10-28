"Owner-only schema/fixture setup; keep owner credentials out of API/Worker/MCP."

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from insightpilot.auth.service import create_account
from insightpilot.checkpoints import setup_checkpoints
from insightpilot.config import Settings
from insightpilot.migrations import configure_role_passwords, migrate, seed_synthetic
from insightpilot.runtime import new_event_loop
from scripts.private_files import replace_private, restrict_existing_private


async def initialize(root, create_accounts=False):
    settings = Settings.from_environment(root)
    owner = os.environ["INSIGHTPILOT_POSTGRES_ADMIN_DSN"]
    with psycopg.connect(owner, autocommit=True, row_factory=dict_row) as conn:
        migrate(conn)
        configure_role_passwords(
            conn,
            {
                role: os.environ["INSIGHTPILOT_PG_" + suffix + "_PASSWORD"]
                for role, suffix in (
                    ("ip_state", "STATE"),
                    ("ip_checkpoint", "CHECKPOINT"),
                    ("ip_reader", "QUERY"),
                )
            },
        )
        version = seed_synthetic(conn)
    async with await psycopg.AsyncConnection.connect(
        owner, autocommit=True, row_factory=dict_row
    ) as conn:
        await setup_checkpoints(conn)
    if create_accounts:
        directory = settings.data_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "accounts.json"
        if path.exists():
            restrict_existing_private(path)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        async with await psycopg.AsyncConnection.connect(
            settings.postgres_state_dsn.get_secret_value(), autocommit=True, row_factory=dict_row
        ) as conn:
            for tenant, email, name, role in (
                (version.tenant_ids[0], "admin@a.synthetic.invalid", "本地管理员", "admin"),
                (version.tenant_ids[0], "analyst@a.synthetic.invalid", "商户甲分析师", "analyst"),
                (version.tenant_ids[1], "analyst@b.synthetic.invalid", "商户乙分析师", "analyst"),
            ):
                found = await (
                    await conn.execute(
                        "SELECT id FROM app_state.users WHERE tenant_id=%s AND email=%s",
                        (tenant, email),
                    )
                ).fetchone()
                if found:
                    continue
                password = secrets.token_urlsafe(24)
                user = await create_account(conn, tenant, email, name, role, password)
                existing.append(
                    {
                        "tenant_id": str(tenant),
                        "user_id": str(user),
                        "email": email,
                        "role": role,
                        "password": password,
                    }
                )
                replace_private(path, json.dumps(existing, ensure_ascii=False, indent=2).encode())
    return {
        "initialized": True,
        "schema_version": 10,
        "dataset_version": str(version.id),
        "synthetic": True,
        "existing_accounts_preserved": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--create-accounts", action="store_true")
    args = parser.parse_args()
    with asyncio.Runner(loop_factory=new_event_loop) as runner:
        print(json.dumps(runner.run(initialize(args.workspace_root, args.create_accounts))))
