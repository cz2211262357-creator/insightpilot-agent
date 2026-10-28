"Explicit fictional identities used only in disposable fixture containers."

import asyncio
from functools import cache
from uuid import NAMESPACE_DNS, uuid5

import psycopg
from psycopg.rows import dict_row
from pwdlib import PasswordHash

from insightpilot.migrations import migrate, seed_synthetic
from insightpilot.runtime import new_event_loop
from insightpilot.types import Actor, Role

PASSWORD = "Synthetic-Tests-Only#2026"


def dsn(dep, role="ip_state"):
    password = dep.owner_password if role == "postgres" else dep.password
    return (
        f"host=127.0.0.1 port={dep.port} user={role} "
        f"password={password} dbname=insightpilot_fixture"
    )


@cache
def password_hash():
    return PasswordHash.recommended().hash(PASSWORD)


def prepare_users(dep):
    with psycopg.connect(dsn(dep, "postgres"), autocommit=True, row_factory=dict_row) as conn:
        migrate(conn)
        version = seed_synthetic(conn)
        actors = {}
        for label, tenant in zip(("A", "B"), version.tenant_ids, strict=True):
            for kind in ("admin", "analyst", "other"):
                role = Role.ADMIN if kind == "admin" else Role.ANALYST
                user_id = uuid5(NAMESPACE_DNS, f"insightpilot.synthetic.{kind}.{label}")
                current = conn.execute(
                    (
                        "INSERT INTO "
                        "app_state.users(id,tenant_id,email,display_name,password_hash,role) "
                        "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET "
                        "role=EXCLUDED.role,enabled=true,password_hash=EXCLUDED.password_hash "
                        "RETURNING auth_version"
                    ),
                    (
                        user_id,
                        tenant,
                        f"{kind}@{label.lower()}.synthetic.invalid",
                        f"虚构{label}-{kind}",
                        password_hash(),
                        role.value,
                    ),
                ).fetchone()
                conn.execute("DELETE FROM app_state.sessions WHERE user_id=%s", (user_id,))
                actors[f"{label}_{kind}"] = Actor(tenant, user_id, role, current["auth_version"])
        return version, actors


def run(coro):
    with asyncio.Runner(loop_factory=new_event_loop) as runner:
        return runner.run(coro)


def make_conversation(dep, actor):
    from uuid import uuid4

    conversation, topic_a, topic_b = uuid4(), uuid4(), uuid4()
    with psycopg.connect(dsn(dep), autocommit=True) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO app_state.conversations(id,tenant_id,user_id) VALUES(%s,%s,%s)",
            (conversation, actor.tenant_id, actor.user_id),
        )
        for topic in (topic_a, topic_b):
            conn.execute(
                "INSERT INTO app_state.topics(id,tenant_id,conversation_id,title) "
                "VALUES(%s,%s,%s,%s)",
                (topic, actor.tenant_id, conversation, "Synthetic analysis"),
            )
        conn.execute(
            "UPDATE app_state.conversations SET active_topic_id=%s WHERE id=%s",
            (topic_a, conversation),
        )
    return conversation, topic_a, topic_b
