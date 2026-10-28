from uuid import uuid4

import pytest

from insightpilot.errors import DomainError
from insightpilot.runs.models import Scope
from insightpilot.runs.repository import RunRepository
from insightpilot.runs.service import RunService
from tests.integration.test_run_races import force_expired, pool
from tests.integration.test_run_races import scenario as scenario
from tests.support import run


@pytest.mark.parametrize("kind,maximum", [("model", 8), ("query", 4), ("repair", 2)])
def test_persistent_reservation_has_hard_limit_and_terminal_failure(scenario, kind, maximum):
    dep, actors, (conversation, a, b) = scenario

    async def check():
        async with pool(dep) as connections:
            service = RunService(connections)
            repo = RunRepository(connections)
            receipt = await service.submit(
                actors["A_admin"], conversation, a, uuid4(), "预算测试", Scope.BACKGROUND
            )
            fence = await repo.claim(receipt.run_id, uuid4())
            assert fence is not None
            for expected in range(1, maximum + 1):
                remaining = await repo.reserve(fence, kind)
                assert getattr(remaining, kind + "_used") == expected
            with pytest.raises(DomainError) as exhausted:
                await repo.reserve(fence, kind)
            assert exhausted.value.code == "run_budget_exhausted"
            status = await service.get(actors["A_admin"], receipt.run_id)
            assert status.state == "FAILED" and getattr(status, kind + "_used") == maximum

    run(check())


def test_recovery_keeps_budget_deadline_and_exactly_three_auto_attempts(scenario):
    dep, actors, (conversation, a, b) = scenario

    async def check():
        async with pool(dep) as connections:
            service = RunService(connections)
            repo = RunRepository(connections)
            receipt = await service.submit(
                actors["A_admin"], conversation, a, uuid4(), "恢复预算", Scope.BACKGROUND
            )
            first = await repo.claim(receipt.run_id, uuid4())
            assert first is not None
            await repo.reserve(first, "model")
            await repo.reserve(first, "model")
            async with connections.connection() as conn:
                deadline = (
                    await (
                        await conn.execute(
                            "SELECT active_deadline FROM app_state.runs WHERE id=%s",
                            (receipt.run_id,),
                        )
                    ).fetchone()
                )["active_deadline"]
            force_expired(dep, receipt.run_id)
            second = await repo.claim(receipt.run_id, uuid4())
            assert second is not None
            assert (await repo.reserve(second, "model")).model_used == 3
            await repo.fail(second, "provider_unavailable", retriable=True)
            assert await repo.claim(receipt.run_id, uuid4()) is None
            force_expired(dep, receipt.run_id, "available_at")
            third = await repo.claim(receipt.run_id, uuid4())
            assert third is not None
            await repo.fail(third, "provider_unavailable", retriable=True)
            status = await service.get(actors["A_admin"], receipt.run_id)
            assert status.state == "FAILED" and status.attempts == 3 and status.model_used == 3
            async with connections.connection() as conn:
                actual = (
                    await (
                        await conn.execute(
                            "SELECT active_deadline FROM app_state.runs WHERE id=%s",
                            (receipt.run_id,),
                        )
                    ).fetchone()
                )["active_deadline"]
            assert actual == deadline

    run(check())


def test_hitl_resume_preserves_time_usage_and_does_not_spend_auto_retry(scenario):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as connections:
            service = RunService(connections)
            repo = RunRepository(connections)
            receipt = await service.submit(
                actor, conversation, a, uuid4(), "待确认报告", Scope.BACKGROUND
            )
            first = await repo.claim(receipt.run_id, uuid4())
            assert first is not None
            await repo.reserve(first, "model")
            await repo.reserve(first, "query")
            await repo.pause(first)
            async with connections.connection() as conn, conn.transaction():
                row = await (
                    await conn.execute(
                        "SELECT * FROM app_state.runs WHERE id=%s FOR UPDATE", (receipt.run_id,)
                    )
                ).fetchone()
                assert row["state"] == "WAITING_INPUT" and row["lease_until"] is None
                assert 0 < row["remaining_seconds"] <= 300
                saved = row["remaining_seconds"]
                # Internal-only primitive. Task6 exposes resume only with bound approval.
                await repo._resume_locked(conn, row)
            second = await repo.claim(receipt.run_id, uuid4())
            assert second is not None
            assert second.execution_version > first.execution_version
            remaining = await repo.reserve(second, "model")
            assert remaining.model_used == 2 and remaining.query_used == 1
            async with connections.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT attempts,total_claims,remaining_seconds FROM "
                        "app_state.runs WHERE id=%s",
                        (receipt.run_id,),
                    )
                ).fetchone()
                assert (
                    row["attempts"] == 1
                    and row["total_claims"] == 2
                    and row["remaining_seconds"] == saved
                )

    run(check())


@pytest.mark.parametrize("field", ["queue_until", "active_deadline"])
def test_expired_queue_or_absolute_execution_is_terminal(scenario, field):
    dep, actors, (conversation, a, b) = scenario

    async def check():
        async with pool(dep) as connections:
            service = RunService(connections)
            repo = RunRepository(connections)
            receipt = await service.submit(
                actors["A_admin"], conversation, a, uuid4(), "截止时间", Scope.BACKGROUND
            )
            if field == "active_deadline":
                assert await repo.claim(receipt.run_id, uuid4()) is not None
            force_expired(dep, receipt.run_id, field)
            assert await repo.claim(receipt.run_id, uuid4()) is None
            assert (await service.get(actors["A_admin"], receipt.run_id)).state == "TIMED_OUT"

    run(check())
