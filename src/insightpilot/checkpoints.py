"Official checkpoint writes and current authority share one connection/transaction."

import hashlib
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

import psycopg
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Interrupt
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout

from insightpilot.errors import DomainError
from insightpilot.types import Actor, RunFence


@dataclass(frozen=True)
class CheckpointScope:
    actor: Actor
    fence: RunFence
    thread_id: str


_SCOPE = ContextVar("insightpilot_checkpoint_scope", default=None)


def strict_serializer():
    # Explicit allowlist avoids the library's legacy permissive environment default.
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=[
            ("builtins", "RuntimeError"),
            ("builtins", "ValueError"),
            ("builtins", "TimeoutError"),
        ],
    )


@contextmanager
def checkpoint_scope(actor, fence, thread_id):
    if actor.tenant_id != fence.tenant_id or actor.auth_version != fence.auth_version:
        raise DomainError("checkpoint_context_mismatch", 409)
    scope = CheckpointScope(actor, fence, str(UUID(str(thread_id))))
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def current_checkpoint_scope():
    scope = _SCOPE.get()
    if scope is None:
        raise DomainError("checkpoint_context_required", 409)
    return scope


def scoped_config(config):
    scope = _SCOPE.get()
    if scope is None:
        raise DomainError("checkpoint_context_required", 409)
    configurable = dict((config or {}).get("configurable", {}))
    if (
        configurable.get("thread_id") != scope.thread_id
        or configurable.get("checkpoint_ns", "") != ""
    ):
        raise DomainError("checkpoint_context_mismatch", 409)
    configurable["checkpoint_ns"] = ""
    return scope, {**config, "configurable": configurable}


def plain(value, depth=0):
    if depth > 64:
        raise ValueError("checkpoint_value_depth")
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, Interrupt):
        plain(value.value, depth + 1)
        plain(value.id, depth + 1)
        return
    if type(value) in (RuntimeError, ValueError, TimeoutError):
        if (
            len(value.args) != 1
            or not isinstance(value.args[0], str)
            or not re.fullmatch(r"[a-z0-9_]{1,128}", value.args[0])
        ):
            raise ValueError("checkpoint_error_not_sanitized")
        return
    if type(value) in (list, tuple):
        for item in value:
            plain(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("checkpoint_key_type")
            plain(item, depth + 1)
        return
    raise ValueError("checkpoint_custom_type_forbidden")


async def setup_checkpoints(conn):
    "Owner-only maintenance. Runtime saver setup is deliberately unavailable."
    if not conn.autocommit or conn.row_factory is not dict_row:
        raise ValueError("checkpoint_setup_requires_autocommit_dict_row")
    info = await (
        await conn.execute("""
        SELECT current_user AS username,current_database() AS database,rolsuper
        FROM pg_roles WHERE rolname=current_user
        """)
    ).fetchone()
    if info["database"] not in ("insightpilot", "insightpilot_fixture"):
        raise ValueError("checkpoint_setup_database_scope")
    if not info["rolsuper"] and info["username"] != "ip_owner":
        raise DomainError("checkpoint_setup_owner_required", 403)
    previous = (
        await (await conn.execute("SELECT current_setting('search_path') AS path")).fetchone()
    )["path"]
    await conn.execute("SET ROLE ip_owner")
    try:
        await conn.execute("SET search_path=graph_checkpoint,pg_catalog")
        await AsyncPostgresSaver(conn, serde=strict_serializer()).setup()
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute("SELECT set_config('search_path',%s,false)", (previous,))


class FencedPostgresSaver(BaseCheckpointSaver):
    "Compose public official APIs; never call a pool-backed writer after a state check."

    def __init__(self, pool):
        super().__init__(serde=strict_serializer())
        self.pool = pool
        # The pool-backed instance supplies only the public pure version generator.
        self._versions = AsyncPostgresSaver(pool, serde=self.serde)

    def get_next_version(self, current, channel):
        return self._versions.get_next_version(current, channel)

    async def _guard(self, conn, scope, config, digest=None):
        actor, fence = scope.actor, scope.fence
        row = await (
            await conn.execute(
                """
            SELECT app_state.guard_checkpoint(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS allowed
            """,
                (
                    actor.user_id,
                    actor.role.value,
                    fence.tenant_id,
                    fence.run_id,
                    fence.topic_id,
                    fence.task_revision,
                    fence.execution_version,
                    fence.owner,
                    fence.auth_version,
                    config["configurable"]["thread_id"],
                    config["configurable"]["checkpoint_ns"],
                    digest,
                ),
            )
        ).fetchone()
        if not row["allowed"]:
            raise DomainError("stale_checkpoint_execution", 409)

    async def _transaction(self, config, operation, digest=None):
        scope, config = scoped_config(config)
        try:
            async with self.pool.connection() as conn, conn.transaction():
                await self._guard(conn, scope, config)
                official = AsyncPostgresSaver(conn, serde=self.serde)
                result = await operation(official, config)
                # Recheck clock/authority after official writes. Any failure rolls
                # checkpoint, blobs, pending writes AND digest back together.
                await self._guard(conn, scope, config, digest)
                return result
        except (psycopg.Error, PoolTimeout):
            raise DomainError("checkpoint_unavailable", 503) from None

    def _digest(self, kind, payload):
        plain(payload)
        tag, data = self.serde.dumps_typed({"kind": kind, "payload": payload})
        return hashlib.sha256(tag.encode() + b":" + data).hexdigest()

    async def aget_tuple(self, config):
        async def read(official, normalized):
            return await official.aget_tuple(normalized)

        return await self._transaction(config, read)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        if before is not None:
            scoped_config(before)
        limit = 20 if limit is None else limit
        if type(limit) is not int or not 0 <= limit <= 100:
            raise DomainError("checkpoint_list_limit", 422)

        async def read(official, normalized):
            return [
                item
                async for item in official.alist(
                    normalized, filter=filter, before=before, limit=limit
                )
            ]

        # Buffer under the guard; never keep authority locks while a consumer awaits.
        for item in await self._transaction(config, read):
            yield item

    async def aput(self, config, checkpoint, metadata, new_versions):
        digest = self._digest(
            "checkpoint", {"checkpoint": checkpoint, "metadata": metadata, "versions": new_versions}
        )

        async def write(official, normalized):
            return await official.aput(normalized, checkpoint, metadata, new_versions)

        return await self._transaction(config, write, digest)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        digest = self._digest(
            "writes", {"writes": list(writes), "task_id": task_id, "task_path": task_path}
        )

        async def write(official, normalized):
            return await official.aput_writes(normalized, writes, task_id, task_path)

        return await self._transaction(config, write, digest)

    async def setup(self, *args, **kwargs):
        raise DomainError("checkpoint_runtime_setup_forbidden", 403)

    async def acopy_thread(self, *args, **kwargs):
        raise DomainError("checkpoint_operation_forbidden", 403)

    async def adelete_thread(self, *args, **kwargs):
        raise DomainError("checkpoint_operation_forbidden", 403)

    async def adelete_for_runs(self, *args, **kwargs):
        raise DomainError("checkpoint_operation_forbidden", 403)

    async def aprune(self, *args, **kwargs):
        raise DomainError("checkpoint_operation_forbidden", 403)

    async def aget_delta_channel_history(self, *args, **kwargs):
        raise DomainError("checkpoint_delta_channel_forbidden", 403)

    def put(self, *args, **kwargs):
        raise NotImplementedError("asynchronous fenced API required")

    def put_writes(self, *args, **kwargs):
        raise NotImplementedError("asynchronous fenced API required")

    def copy_thread(self, *args, **kwargs):
        raise NotImplementedError("checkpoint operation forbidden")

    def delete_thread(self, *args, **kwargs):
        raise NotImplementedError("checkpoint operation forbidden")

    def delete_for_runs(self, *args, **kwargs):
        raise NotImplementedError("checkpoint operation forbidden")

    def prune(self, *args, **kwargs):
        raise NotImplementedError("checkpoint operation forbidden")
