import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from insightpilot.errors import DomainError
from insightpilot.runs.models import Scope
from insightpilot.runs.repository import RunRepository, require_fence
from insightpilot.runs.service import RunService
from tests.support import dsn, make_conversation, prepare_users, run


@pytest.fixture
def scenario(real_dependencies):
    dep = real_dependencies
    version, actors = prepare_users(dep)
    return dep, actors, make_conversation(dep, actors["A_admin"])


def pool(dep):
    return AsyncConnectionPool(
        dsn(dep),
        open=False,
        min_size=0,
        max_size=8,
        timeout=10,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )


def force_expired(dep, run_id, field="lease_until", seconds=None):
    assert field in ("lease_until", "queue_until", "active_deadline", "available_at")
    with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
        conn.execute(
            f"UPDATE app_state.runs SET {field}=clock_timestamp()-"
            "%s*interval '1 second' WHERE id=%s",
            (seconds if seconds is not None else (31 if field == "lease_until" else 1), run_id),
        )


def test_twenty_retries_one_message_run_epoch_and_immutable_receipt(scenario):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]
    key = uuid4()

    async def check():
        async with pool(dep) as connections:
            service = RunService(connections)
            receipts = await asyncio.gather(
                *(
                    service.submit(actor, conversation, a, key, "八月销售额", Scope.BACKGROUND)
                    for _ in range(20)
                )
            )
            assert len({r.run_id for r in receipts}) == 1
            assert len({r.message_id for r in receipts}) == 1
            assert all(r.epoch == 1 for r in receipts)
            async with connections.connection() as conn:
                count = await (
                    await conn.execute(
                        "SELECT count(*) AS n FROM app_state.messages WHERE conversation_id=%s",
                        (conversation,),
                    )
                ).fetchone()
                assert count["n"] == 1
                assert (
                    await (
                        await conn.execute(
                            "SELECT count(*) AS n FROM app_state.runs WHERE conversation_id=%s",
                            (conversation,),
                        )
                    ).fetchone()
                )["n"] == 1
                assert (
                    await (
                        await conn.execute(
                            "SELECT epoch FROM app_state.conversations WHERE id=%s", (conversation,)
                        )
                    ).fetchone()
                )["epoch"] == 1
            await service.switch_topic(actor, conversation, b, 1)
            again = await service.submit(
                actor, conversation, a, key, "八月销售额", Scope.BACKGROUND
            )
            assert again == receipts[0]
            async with connections.connection() as conn:
                assert (
                    await (
                        await conn.execute(
                            "SELECT epoch FROM app_state.conversations WHERE id=%s", (conversation,)
                        )
                    ).fetchone()
                )["epoch"] == 2
            with pytest.raises(DomainError) as conflict:
                await service.submit(actor, conversation, a, key, "九月销售额", Scope.BACKGROUND)
            assert conflict.value.status == 409

    run(check())


def test_two_tabs_cas_and_a_b_a_does_not_invalidate_background(scenario):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as connections:
            service = RunService(connections)
            repo = RunRepository(connections)
            receipt = await service.submit(
                actor, conversation, a, uuid4(), "后台报告", Scope.BACKGROUND
            )
            fence = await repo.claim(receipt.run_id, uuid4())
            assert fence is not None
            results = await asyncio.gather(
                *(service.switch_topic(actor, conversation, b, 1) for _ in range(2)),
                return_exceptions=True,
            )
            assert sum(not isinstance(x, Exception) for x in results) == 1
            assert sum(isinstance(x, DomainError) and x.status == 409 for x in results) == 1
            returned = await service.switch_topic(actor, conversation, a, 2)
            assert returned.epoch == 3
            async with connections.connection() as conn, conn.transaction():
                await require_fence(conn, fence)
            assert await repo.renew(fence)

    run(check())


def test_expired_lease_rejects_old_writer_before_and_after_new_claim(scenario):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as connections:
            receipt = await RunService(connections).submit(
                actor, conversation, a, uuid4(), "报告", Scope.BACKGROUND
            )
            repo = RunRepository(connections)
            old = await repo.claim(receipt.run_id, uuid4())
            assert old is not None
            force_expired(dep, receipt.run_id)
            async with connections.connection() as conn:
                with pytest.raises(DomainError):
                    async with conn.transaction():
                        await require_fence(conn, old)
            assert await repo.renew(old) is False
            new = await repo.claim(receipt.run_id, uuid4())
            assert new is not None and new.execution_version == old.execution_version + 1
            with pytest.raises(DomainError):
                await repo.reserve(old, "model")
            remaining = await repo.reserve(new, "model")
            assert remaining.model_used == 1

    run(check())


def test_cancel_revise_and_old_foreground_fence_are_rejected(scenario):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as connections:
            service = RunService(connections)
            repo = RunRepository(connections)
            receipt = await service.submit(
                actor, conversation, a, uuid4(), "前台问题", Scope.INTERACTIVE
            )
            fence = await repo.claim(receipt.run_id, uuid4())
            assert fence is not None
            await service.switch_topic(actor, conversation, b, 1)
            with pytest.raises(DomainError):
                await repo.reserve(fence, "model")
            original = await service.submit(
                actor, conversation, b, uuid4(), "报告初版", Scope.BACKGROUND
            )
            key = uuid4()
            revised = await service.revise(actor, original.run_id, 1, key, "报告修订版")
            assert revised.run_id != original.run_id and revised.task_revision == 2
            assert await service.revise(actor, original.run_id, 1, key, "报告修订版") == revised
            async with connections.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT id,state,thread_id,supersedes_run_id FROM app_state.runs "
                        "WHERE id=ANY(%s)",
                        ([original.run_id, revised.run_id],),
                    )
                ).fetchall()
            by_id = {r["id"]: r for r in rows}
            assert by_id[original.run_id]["state"] == "SUPERSEDED"
            assert by_id[revised.run_id]["supersedes_run_id"] == original.run_id
            assert by_id[original.run_id]["thread_id"] != by_id[revised.run_id]["thread_id"]
            cancelled = await service.cancel(actor, revised.run_id, 2)
            assert cancelled.state == "CANCELLED" and cancelled.task_revision == 3

    run(check())


def test_other_user_and_tenant_cannot_read_or_submit_owned_topic(scenario):
    dep, actors, (conversation, a, b) = scenario

    async def check():
        async with pool(dep) as connections:
            service = RunService(connections)
            receipt = await service.submit(
                actors["A_admin"], conversation, a, uuid4(), "报告", Scope.BACKGROUND
            )
            for actor in (actors["A_other"], actors["B_admin"]):
                with pytest.raises(DomainError) as denied:
                    await service.get(actor, receipt.run_id)
                assert denied.value.status == 404
                with pytest.raises(DomainError):
                    await service.submit(actor, conversation, a, uuid4(), "窃取", Scope.BACKGROUND)

    run(check())


def test_twenty_workers_only_one_can_claim_and_guard_requires_outer_transaction(scenario):
    dep, actors, (conversation, a, b) = scenario

    async def check():
        async with pool(dep) as connections:
            receipt = await RunService(connections).submit(
                actors["A_admin"], conversation, a, uuid4(), "单一执行者", Scope.BACKGROUND
            )
            repo = RunRepository(connections)
            claims = await asyncio.gather(*(repo.claim(receipt.run_id, uuid4()) for _ in range(20)))
            winners = [value for value in claims if value is not None]
            assert len(winners) == 1
            assert connections.get_stats()["connections_num"] >= 2
            async with connections.connection() as conn:
                with pytest.raises(RuntimeError, match="requires_transaction"):
                    await require_fence(conn, winners[0])

    run(check())


def test_crash_recovery_observes_five_second_first_backoff(scenario):
    dep, actors, (conversation, a, b) = scenario

    async def check():
        async with pool(dep) as connections:
            receipt = await RunService(connections).submit(
                actors["A_admin"], conversation, a, uuid4(), "故障退避", Scope.BACKGROUND
            )
            repo = RunRepository(connections)
            assert await repo.claim(receipt.run_id, uuid4()) is not None
            force_expired(dep, receipt.run_id, seconds=1)
            assert await repo.claim(receipt.run_id, uuid4()) is None
            force_expired(dep, receipt.run_id, seconds=6)
            assert await repo.claim(receipt.run_id, uuid4()) is not None

    run(check())


def test_checkpoint_role_guard_is_private_and_paused_transaction_does_not_hold_locks_forever(
    scenario,
):
    dep, actors, (conversation, a, b) = scenario

    async def check():
        async with pool(dep) as connections:
            receipt = await RunService(connections).submit(
                actors["A_admin"], conversation, a, uuid4(), "检查点边界", Scope.BACKGROUND
            )
            fence = await RunRepository(connections).claim(receipt.run_id, uuid4())
            assert fence is not None
            async with await psycopg.AsyncConnection.connect(
                dsn(dep, "ip_checkpoint"), autocommit=True, row_factory=dict_row
            ) as checkpoint:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    await checkpoint.execute("SELECT * FROM app_state.runs")
                with pytest.raises(psycopg.errors.IdleInTransactionSessionTimeout):
                    async with checkpoint.transaction():
                        await require_fence(checkpoint, fence)
                        await asyncio.sleep(5.3)
                        await checkpoint.execute("SELECT 1")
            # The database ends the abandoned transaction; a different connection is live.
            assert await RunRepository(connections).renew(fence)

    run(check())


def test_revocation_during_execution_fails_closed_and_never_retries(scenario):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as connections:
            service = RunService(connections)
            repo = RunRepository(connections)
            receipt = await service.submit(
                actor, conversation, a, uuid4(), "撤权报告", Scope.BACKGROUND
            )
            fence = await repo.claim(receipt.run_id, uuid4())
            assert fence is not None
            with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                admin.execute(
                    "UPDATE app_state.users SET enabled=false WHERE id=%s", (actor.user_id,)
                )
            with pytest.raises(DomainError):
                await repo.reserve(fence, "model")
            assert await repo.claim(receipt.run_id, uuid4()) is None
            with psycopg.connect(
                dsn(dep, "postgres"), autocommit=True, row_factory=dict_row
            ) as admin:
                row = admin.execute(
                    "SELECT state,error_code,attempts,model_used FROM app_state.runs WHERE id=%s",
                    (receipt.run_id,),
                ).fetchone()
            assert row == {
                "state": "FAILED",
                "error_code": "authorization_revoked",
                "attempts": 1,
                "model_used": 0,
            }

    run(check())
