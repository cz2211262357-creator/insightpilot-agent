"Opaque sessions; authorization lock precedes every protected state mutation."

import asyncio
import hashlib
import secrets
from dataclasses import asdict
from uuid import uuid4

from psycopg.types.json import Jsonb

from insightpilot.auth.passwords import dummy_hash, hash_password, verify_password
from insightpilot.errors import DomainError
from insightpilot.types import Actor, Role


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def actor_matches(row, actor):
    return (
        row
        and row["enabled"]
        and row["role"] == actor.role.value
        and row["auth_version"] == actor.auth_version
    )


async def require_current(conn, actor, for_write=False):
    suffix = " FOR SHARE" if for_write else ""
    row = await (
        await conn.execute(
            ("SELECT role,enabled,auth_version FROM app_state.users WHERE tenant_id=%s AND id=%s")
            + suffix,
            (actor.tenant_id, actor.user_id),
        )
    ).fetchone()
    if not actor_matches(row, actor):
        raise DomainError("identity_expired", 401)


async def create_account(conn, tenant_id, email, display_name, role, password):
    user_id = uuid4()
    hashed = await asyncio.to_thread(hash_password, password)
    async with conn.transaction():
        exists = await (
            await conn.execute(
                "SELECT id FROM app_state.users WHERE tenant_id=%s AND email=%s",
                (tenant_id, email.lower()),
            )
        ).fetchone()
        if exists:
            raise DomainError("account_exists", 409)
        await conn.execute(
            (
                "INSERT INTO "
                "app_state.users(id,tenant_id,email,display_name,password_hash,role) "
                "VALUES(%s,%s,%s,%s,%s,%s)"
            ),
            (user_id, tenant_id, email.lower(), display_name, hashed, Role(role).value),
        )
    return user_id


class AuthService:
    def __init__(self, pool, redis=None):
        self.pool = pool
        self.redis = redis

    async def login(self, tenant_id, email, password):
        if self.redis is not None:
            key = "insightpilot:login:" + digest(str(tenant_id) + ":" + email.lower())
            count = await self.redis.eval(
                (
                    "local n=redis.call('INCR',KEYS[1]);if n==1 then "
                    "redis.call('EXPIRE',KEYS[1],60) end;return n"
                ),
                1,
                key,
            )
            if count > 10:
                raise DomainError("login_rate_limited", 429)
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    (
                        "SELECT id,password_hash FROM app_state.users WHERE tenant_id=%s "
                        "AND email=%s"
                    ),
                    (tenant_id, email.lower()),
                )
            ).fetchone()
        hashed = row["password_hash"] if row else await asyncio.to_thread(dummy_hash)
        valid = await asyncio.to_thread(verify_password, password, hashed)
        if not valid or not row:
            raise DomainError("invalid_credentials", 401)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        async with self.pool.connection() as conn, conn.transaction():
            current = await (
                await conn.execute(
                    (
                        "SELECT id,enabled,role,auth_version,password_hash FROM app_state.users "
                        "WHERE tenant_id=%s AND id=%s FOR SHARE"
                    ),
                    (tenant_id, row["id"]),
                )
            ).fetchone()
            if not current or not current["enabled"] or current["password_hash"] != hashed:
                raise DomainError("invalid_credentials", 401)
            actor = Actor(tenant_id, current["id"], Role(current["role"]), current["auth_version"])
            await conn.execute(
                (
                    "INSERT INTO "
                    "app_state.sessions(token_hash,tenant_id,user_id,auth_version,csrf"
                    "_hash,expires_at) "
                    "VALUES(%s,%s,%s,%s,%s,clock_timestamp()+interval '8 hours')"
                ),
                (digest(token), tenant_id, actor.user_id, actor.auth_version, digest(csrf)),
            )
        return actor, token, csrf

    async def authenticate(self, cookie):
        if not cookie or len(cookie) != 43:
            raise DomainError("authentication_required", 401)
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    (
                        "SELECT u.id,u.tenant_id,u.role,u.auth_version,u.enabled,s.auth_ve"
                        "rsion AS "
                        "session_version FROM app_state.sessions s JOIN app_state.users u ON "
                        "u.id=s.user_id AND u.tenant_id=s.tenant_id WHERE s.token_hash=%s AND "
                        "s.expires_at>clock_timestamp()"
                    ),
                    (digest(cookie),),
                )
            ).fetchone()
        if not row or not row["enabled"] or row["auth_version"] != row["session_version"]:
            raise DomainError("identity_expired", 401)
        return Actor(row["tenant_id"], row["id"], Role(row["role"]), row["auth_version"])

    async def check_csrf(self, cookie, csrf):
        await self.authenticate(cookie)
        if not csrf or len(csrf) != 43:
            raise DomainError("csrf_rejected", 403)
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT csrf_hash FROM app_state.sessions WHERE token_hash=%s",
                    (digest(cookie),),
                )
            ).fetchone()
        if not row or not secrets.compare_digest(row["csrf_hash"], digest(csrf)):
            raise DomainError("csrf_rejected", 403)

    async def logout(self, cookie):
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM app_state.sessions WHERE token_hash=%s", (digest(cookie),)
            )

    async def member(self, actor, user_id):
        if actor.role != Role.ADMIN and user_id != actor.user_id:
            raise DomainError("admin_required", 403)
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor)
            row = await (
                await conn.execute(
                    (
                        "SELECT id AS user_id,email,display_name,role,enabled,auth_version FROM "
                        "app_state.users WHERE tenant_id=%s AND id=%s"
                    ),
                    (actor.tenant_id, user_id),
                )
            ).fetchone()
        if not row:
            raise DomainError("member_not_found", 404)
        return row

    async def change_member(self, actor, user_id, enabled=None, role=None):
        if actor.role != Role.ADMIN:
            raise DomainError("admin_required", 403)
        async with self.pool.connection() as conn, conn.transaction():
            rows = await (
                await conn.execute(
                    (
                        "SELECT id,role,enabled,auth_version FROM app_state.users WHERE "
                        "tenant_id=%s AND id=ANY(%s) ORDER BY id FOR UPDATE"
                    ),
                    (actor.tenant_id, [actor.user_id, user_id]),
                )
            ).fetchall()
            by_id = {row["id"]: row for row in rows}
            if not actor_matches(by_id.get(actor.user_id), actor):
                raise DomainError("identity_expired", 401)
            if user_id not in by_id:
                raise DomainError("member_not_found", 404)
            row = await (
                await conn.execute(
                    (
                        "UPDATE app_state.users SET "
                        "enabled=COALESCE(%s,enabled),role=COALESCE(%s,role),auth_version="
                        "auth_version+1 WHERE tenant_id=%s AND id=%s RETURNING id AS "
                        "user_id,email,display_name,role,enabled,auth_version"
                    ),
                    (enabled, role, actor.tenant_id, user_id),
                )
            ).fetchone()
            await conn.execute(
                "DELETE FROM app_state.sessions WHERE tenant_id=%s AND user_id=%s",
                (actor.tenant_id, user_id),
            )
            await conn.execute(
                (
                    "INSERT INTO "
                    "app_state.audits(id,tenant_id,user_id,action,object_id,details) "
                    "VALUES(%s,%s,%s,%s,%s,%s)"
                ),
                (
                    uuid4(),
                    actor.tenant_id,
                    actor.user_id,
                    "member.authorization_changed",
                    user_id,
                    Jsonb(
                        {"enabled": enabled, "role": role, "new_auth_version": row["auth_version"]}
                    ),
                ),
            )
        return row

    @staticmethod
    def public_actor(actor):
        return asdict(actor)
