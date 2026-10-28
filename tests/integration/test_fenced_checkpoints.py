import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from insightpilot.checkpoints import FencedPostgresSaver, checkpoint_scope, setup_checkpoints
from insightpilot.errors import DomainError
from insightpilot.query.gateway import QueryGateway
from insightpilot.runs.models import Scope
from insightpilot.runs.repository import RunRepository
from insightpilot.runs.service import RunService
from tests.integration.test_run_races import force_expired, pool
from tests.integration.test_run_races import scenario as scenario
from tests.support import dsn, run


def checkpoint_pool(dep):
    return AsyncConnectionPool(
        dsn(dep, "ip_checkpoint"),
        open=False,
        min_size=0,
        max_size=3,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "prepare_threshold": 0,
            "options": "-c timezone=UTC -c search_path=graph_checkpoint,pg_catalog",
        },
    )


@pytest.fixture
def checkpoint_case(scenario):
    dep, actors, ids = scenario

    async def setup():
        async with await psycopg.AsyncConnection.connect(
            dsn(dep, "postgres"), autocommit=True, row_factory=dict_row
        ) as conn:
            await setup_checkpoints(conn)

    run(setup())
    return dep, actors, ids


async def begin(state, actor, conversation, topic, scope=Scope.BACKGROUND):
    receipt = await RunService(state).submit(
        actor, conversation, topic, uuid4(), "Synthetic checkpoint acceptance", scope
    )
    fence = await RunRepository(state).claim(receipt.run_id, uuid4())
    assert fence is not None
    async with state.connection() as conn:
        row = await (
            await conn.execute("SELECT thread_id FROM app_state.runs WHERE id=%s", (fence.run_id,))
        ).fetchone()
    return fence, str(row["thread_id"])


def sample(saver, marker, current=None):
    value = empty_checkpoint()
    version = saver.get_next_version(current, None)
    value["channel_values"] = {"state": {"marker": marker}}
    value["channel_versions"] = {"state": version}
    return value, {"state": version}


def counts(dep, thread):
    with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
        return {
            name: conn.execute(
                f"SELECT count(*) FROM graph_checkpoint.{name} WHERE thread_id=%s", (thread,)
            ).fetchone()[0]
            for name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        }


@pytest.mark.parametrize("operation", ["checkpoint", "writes"])
def test_expired_without_new_owner_and_late_after_new_owner_never_write(checkpoint_case, operation):
    dep, actors, (conversation, a, b) = checkpoint_case
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as state, checkpoint_pool(dep) as checkpoints:
            fence, thread = await begin(state, actor, conversation, a)
            saver = FencedPostgresSaver(checkpoints)
            config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
            with checkpoint_scope(actor, fence, thread):
                first, versions = sample(saver, "first")
                stored = await saver.aput(
                    config, first, {"source": "input", "step": 0, "parents": {}}, versions
                )
            force_expired(dep, fence.run_id, seconds=6)

            async def late():
                with checkpoint_scope(actor, fence, thread):
                    if operation == "checkpoint":
                        item, v = sample(saver, "late", versions["state"])
                        await saver.aput(
                            stored, item, {"source": "loop", "step": 1, "parents": {}}, v
                        )
                    else:
                        await saver.aput_writes(
                            stored, [("state", {"marker": "late"})], str(uuid4())
                        )

            before = counts(dep, thread)
            with pytest.raises(DomainError):
                await late()
            assert counts(dep, thread) == before
            fresh = await RunRepository(state).claim(fence.run_id, uuid4())
            assert fresh is not None
            with checkpoint_scope(actor, fresh, thread):
                latest, v = sample(saver, "new-owner", versions["state"])
                await saver.aput(stored, latest, {"source": "loop", "step": 2, "parents": {}}, v)
            after = counts(dep, thread)
            with pytest.raises(DomainError):
                await late()
            assert counts(dep, thread) == after
            with checkpoint_scope(actor, fresh, thread):
                result = await saver.aget_tuple(config)
                assert result.checkpoint["channel_values"]["state"]["marker"] == "new-owner"

    run(check())


@pytest.mark.parametrize("operation", ["checkpoint", "writes"])
def test_outer_rollback_covers_official_checkpoint_blobs_and_pending_writes(
    checkpoint_case, monkeypatch, operation
):
    dep, actors, (conversation, a, b) = checkpoint_case
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as state, checkpoint_pool(dep) as checkpoints:
            fence, thread = await begin(state, actor, conversation, a)
            saver = FencedPostgresSaver(checkpoints)
            config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
            with checkpoint_scope(actor, fence, thread):
                first, versions = sample(saver, "first")
                stored = await saver.aput(
                    config, first, {"source": "input", "step": 0, "parents": {}}, versions
                )
            method = "aput" if operation == "checkpoint" else "aput_writes"
            original = getattr(AsyncPostgresSaver, method)
            seen = []

            async def fault(official, *args, **kwargs):
                seen.append(isinstance(official.conn, psycopg.AsyncConnection))
                await original(official, *args, **kwargs)
                raise RuntimeError("injected_after_official_write")

            monkeypatch.setattr(AsyncPostgresSaver, method, fault)
            before = counts(dep, thread)
            with checkpoint_scope(actor, fence, thread):
                with pytest.raises(RuntimeError, match="injected_after_official_write"):
                    if operation == "checkpoint":
                        item, v = sample(saver, "uncommitted", versions["state"])
                        await saver.aput(
                            stored, item, {"source": "loop", "step": 1, "parents": {}}, v
                        )
                    else:
                        await saver.aput_writes(
                            stored, [("state", {"marker": "uncommitted"})], str(uuid4())
                        )
            assert counts(dep, thread) == before
            assert seen == [True], "official saver must receive the already guarded connection"

    run(check())


def test_thread_namespace_and_current_authorization_are_required_for_reads_and_writes(
    checkpoint_case,
):
    dep, actors, (conversation, a, b) = checkpoint_case
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as state, checkpoint_pool(dep) as checkpoints:
            fence, thread = await begin(state, actor, conversation, a)
            saver = FencedPostgresSaver(checkpoints)
            config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
            with pytest.raises(DomainError):
                await saver.aget_tuple(config)
            with checkpoint_scope(actor, fence, thread):
                with pytest.raises(DomainError):
                    await saver.aget_tuple(
                        {"configurable": {"thread_id": str(uuid4()), "checkpoint_ns": ""}}
                    )
                with pytest.raises(DomainError):
                    await saver.aget_tuple(
                        {"configurable": {"thread_id": thread, "checkpoint_ns": "other-topic"}}
                    )
            with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                admin.execute(
                    "UPDATE app_state.users SET enabled=false WHERE id=%s", (actor.user_id,)
                )
            with checkpoint_scope(actor, fence, thread):
                item, v = sample(saver, "revoked")
                with pytest.raises(DomainError):
                    await saver.aput(config, item, {"source": "input", "step": 0, "parents": {}}, v)
                with pytest.raises(DomainError):
                    await saver.aget_tuple(config)

    run(check())


def test_p03_same_transaction_guard_blocks_takeover_and_expired_old_connection_cannot_commit(
    checkpoint_case, monkeypatch
):
    dep, actors, (conversation, a, b) = checkpoint_case
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as state, checkpoint_pool(dep) as checkpoints:
            fence, thread = await begin(state, actor, conversation, a)
            saver = FencedPostgresSaver(checkpoints)
            config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
            entered = asyncio.Event()
            release = asyncio.Event()
            original = AsyncPostgresSaver.aput

            async def pause(official, cfg, item, *args, **kwargs):
                if item["channel_values"]["state"]["marker"] == "old":
                    entered.set()
                    await release.wait()
                return await original(official, cfg, item, *args, **kwargs)

            monkeypatch.setattr(AsyncPostgresSaver, "aput", pause)

            async def old_write():
                with checkpoint_scope(actor, fence, thread):
                    item, v = sample(saver, "old")
                    await saver.aput(config, item, {"source": "input", "step": 0, "parents": {}}, v)

            old = asyncio.create_task(old_write())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                    with pytest.raises(psycopg.errors.LockNotAvailable):
                        with admin.transaction():
                            admin.execute("SET LOCAL lock_timeout='150ms'")
                            admin.execute(
                                "UPDATE app_state.runs SET lease_until=clock_timestamp()-interval "
                                "'6 seconds' WHERE id=%s",
                                (fence.run_id,),
                            )
                await asyncio.sleep(5.3)
                force_expired(dep, fence.run_id, seconds=6)
                fresh = await RunRepository(state).claim(fence.run_id, uuid4())
                assert fresh is not None
                with checkpoint_scope(actor, fresh, thread):
                    item, v = sample(saver, "new")
                    await saver.aput(config, item, {"source": "loop", "step": 1, "parents": {}}, v)
                release.set()
                with pytest.raises((DomainError, psycopg.Error)):
                    await old
                with checkpoint_scope(actor, fresh, thread):
                    saved = await saver.aget_tuple(config)
                    assert saved.checkpoint["channel_values"]["state"]["marker"] == "new"
            finally:
                release.set()
                if not old.done():
                    try:
                        await old
                    except Exception:
                        pass

    run(check())


def child(dep, workspace_root, actor, fence, thread, action, query_id, approval_id=None):
    from dataclasses import asdict

    payload = {
        **asdict(fence),
        "tenant_id": str(actor.tenant_id),
        "user_id": str(actor.user_id),
        "role": actor.role.value,
        "thread_id": thread,
        "action": action,
        "query_id": str(query_id),
        "approval_id": str(approval_id) if approval_id else None,
    }
    payload = {
        key: str(value) if hasattr(value, "hex") else value for key, value in payload.items()
    }
    env = os.environ.copy()
    env["INSIGHTPILOT_POSTGRES_CHECKPOINT_DSN"] = dsn(dep, "ip_checkpoint")
    result = subprocess.run(
        [sys.executable, "-m", "tests.checkpoint_process"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        (workspace_root / ".cache/insightpilot-verification/checkpoint-child-error.log").write_text(
            result.stderr, encoding="utf-8"
        )
        raise RuntimeError("Checkpoint child failed; inspect private diagnostic log")
    return json.loads(result.stdout)


def test_real_graph_interrupt_two_processes_and_bound_approval_preserve_state(
    checkpoint_case, workspace_root
):
    dep, actors, (conversation, a, b) = checkpoint_case
    actor = actors["A_admin"]

    async def check():
        async with (
            pool(dep) as state,
            AsyncConnectionPool(
                dsn(dep, "ip_reader"),
                open=False,
                kwargs={"autocommit": True, "row_factory": dict_row},
            ) as reader,
        ):
            fence, thread = await begin(state, actor, conversation, a)
            gateway = QueryGateway(
                state, reader, SecretStr("synthetic-checkpoint-confirmation-key-32bytes")
            )
            ticket = await gateway.validate(
                actor,
                fence,
                "SELECT a.order_id AS a_order,b.order_id AS b_order,c.order_id AS "
                "c_order FROM analysis.orders a CROSS JOIN analysis.orders b "
                "CROSS JOIN analysis.orders c",
                {},
            )
            assert ticket.requires_approval
            first = child(dep, workspace_root, actor, fence, thread, "start", ticket.query_id)
            assert first["interrupted"] and first["counter"] == 1 and not first["approved"]
            await RunRepository(state).pause(fence, query_id=ticket.query_id)
            request = await gateway.create_confirmation(actor, fence.run_id, ticket.query_id)
            await gateway.approve(actor, request["confirmation_id"], request["token"])
            fresh = await RunRepository(state).claim(fence.run_id, uuid4())
            assert fresh is not None
            second = child(
                dep,
                workspace_root,
                actor,
                fresh,
                thread,
                "resume",
                ticket.query_id,
                request["confirmation_id"],
            )
            assert not second["interrupted"] and second["approved"] and second["counter"] == 1
            assert second["checkpoint_id"] != first["checkpoint_id"]
            assert (await RunService(state).get(actor, fence.run_id)).state == "RUNNING"

    run(check())


def test_checkpoint_holding_identity_lock_commits_before_revocation_then_refuses_more(
    checkpoint_case, monkeypatch
):
    dep, actors, (conversation, a, b) = checkpoint_case
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as state, checkpoint_pool(dep) as checkpoints:
            fence, thread = await begin(state, actor, conversation, a)
            saver = FencedPostgresSaver(checkpoints)
            config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
            entered = asyncio.Event()
            release = asyncio.Event()
            original = AsyncPostgresSaver.aput

            async def pause(official, *args, **kwargs):
                entered.set()
                await release.wait()
                return await original(official, *args, **kwargs)

            monkeypatch.setattr(AsyncPostgresSaver, "aput", pause)

            async def write():
                with checkpoint_scope(actor, fence, thread):
                    value, v = sample(saver, "valid-before-revocation")
                    return await saver.aput(
                        config, value, {"source": "input", "step": 0, "parents": {}}, v
                    )

            pending = asyncio.create_task(write())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                    with pytest.raises(psycopg.errors.LockNotAvailable):
                        with admin.transaction():
                            admin.execute("SET LOCAL lock_timeout='150ms'")
                            admin.execute(
                                "UPDATE app_state.users SET enabled=false WHERE id=%s",
                                (actor.user_id,),
                            )
                release.set()
                await pending
                assert counts(dep, thread)["checkpoints"] == 1
                with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                    admin.execute(
                        "UPDATE app_state.users SET enabled=false WHERE id=%s", (actor.user_id,)
                    )
                with checkpoint_scope(actor, fence, thread):
                    with pytest.raises(DomainError):
                        await saver.aget_tuple(config)
            finally:
                release.set()
                if not pending.done():
                    await pending

    run(check())


def test_shared_saver_contexts_are_isolated_and_background_survives_topic_switch(checkpoint_case):
    from tests.support import make_conversation

    dep, actors, (conversation, a, b) = checkpoint_case
    actor = actors["A_admin"]
    cb, tb, _ = make_conversation(dep, actors["B_admin"])

    async def check():
        async with pool(dep) as state, checkpoint_pool(dep) as checkpoints:
            fa, ta = await begin(state, actor, conversation, a)
            fb, tb_string = await begin(state, actors["B_admin"], cb, tb, Scope.INTERACTIVE)
            await RunService(state).switch_topic(actor, conversation, b, 1)
            saver = FencedPostgresSaver(checkpoints)

            async def scoped(identity, fence, thread, label):
                with checkpoint_scope(identity, fence, thread):
                    await asyncio.sleep(0)
                    cfg = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
                    item, v = sample(saver, label)
                    await saver.aput(cfg, item, {"source": "input", "step": 0, "parents": {}}, v)
                    rows = [entry async for entry in saver.alist(cfg, limit=10)]
                    assert (
                        len(rows) == 1
                        and rows[0].checkpoint["channel_values"]["state"]["marker"] == label
                    )

            await asyncio.gather(
                scoped(actor, fa, ta, "SYN-A"), scoped(actors["B_admin"], fb, tb_string, "SYN-B")
            )

    run(check())


def test_owner_only_setup_unsupported_mutators_and_checkpoint_role_state_boundary(checkpoint_case):
    dep, actors, (conversation, a, b) = checkpoint_case
    expected = {
        "aput",
        "aput_writes",
        "acopy_thread",
        "adelete_thread",
        "adelete_for_runs",
        "aprune",
    }
    actual = {
        name
        for name in dir(AsyncPostgresSaver)
        if name.startswith("a") and any(word in name for word in ("put", "copy", "delete", "prune"))
    }
    assert actual == expected, "new official mutator must be reviewed before library upgrade"

    async def check():
        async with pool(dep) as state, checkpoint_pool(dep) as checkpoints:
            saver = FencedPostgresSaver(checkpoints)
            async with state.connection() as conn:
                with pytest.raises(DomainError) as forbidden:
                    await setup_checkpoints(conn)
                assert forbidden.value.code == "checkpoint_setup_owner_required"
            for method in (
                "setup",
                "acopy_thread",
                "adelete_thread",
                "adelete_for_runs",
                "aprune",
                "aget_delta_channel_history",
            ):
                with pytest.raises(DomainError):
                    await getattr(saver, method)("untrusted")
            async with checkpoints.connection() as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    await conn.execute("UPDATE app_state.runs SET model_used=0")

    run(check())
