import asyncio
from uuid import uuid4

import psycopg
import pytest

from insightpilot.errors import DomainError
from insightpilot.runs.repository import RunRepository
from insightpilot.runs.service import RunService
from tests.integration.test_query_gateway import start, systems
from tests.integration.test_run_races import pool
from tests.integration.test_run_races import scenario as scenario
from tests.support import dsn, run


def test_old_assistant_history_is_authorized_beyond_run_page(scenario):
    dep, actors, (conversation, topic, _) = scenario
    actor = actors["A_admin"]

    async def check():
        async with pool(dep) as state:
            service = RunService(state)
            receipt = await service.submit(
                actor, conversation, topic, uuid4(), "original result", "BACKGROUND"
            )
            fence = await RunRepository(state).claim(receipt.run_id, uuid4())
            summary = "sensitive historical value 1234.56"
            await RunRepository(state).finish(fence, {"summary": summary})
            for _ in range(100):
                later = await service.submit(
                    actor, conversation, topic, uuid4(), "later cancelled request", "BACKGROUND"
                )
                await service.cancel(actor, later.run_id, later.task_revision)
            visible = await service.topic_history(actor, topic)
            assert len(visible["runs"]) == 100
            assert receipt.run_id not in {row.run_id for row in visible["runs"]}
            assert any(m["content"] == summary for m in visible["messages"])
            with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                admin.execute(
                    "UPDATE app_state.dataset_versions SET active=false WHERE tenant_id=%s",
                    (actor.tenant_id,),
                )
            try:
                hidden = await service.topic_history(actor, topic)
                old = [m for m in hidden["messages"] if m["run_id"] == receipt.run_id]
                assert len(old) == 2, "The report stays in the message page, outside the run page"
                assert all(m["content"] != summary for m in old), "Historical report leaked"
            finally:
                with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                    admin.execute(
                        "UPDATE app_state.dataset_versions SET active=true WHERE tenant_id=%s",
                        (actor.tenant_id,),
                    )

    run(check())


@pytest.mark.parametrize("field", ["lease_until", "active_deadline"])
def test_final_report_transition_rejects_clock_expiry_after_guard(scenario, monkeypatch, field):
    dep, actors, (conversation, topic, _) = scenario
    actor = actors["A_admin"]
    actual_execute = psycopg.AsyncConnection.execute
    injected = []

    async def check():
        async with pool(dep) as state:
            fence = await start(state, actor, conversation, topic)

            async def delayed(self, sql, params=None, **kwargs):
                if isinstance(sql, str) and "state='SUCCEEDED',report=" in sql:
                    await actual_execute(
                        self,
                        f"UPDATE app_state.runs SET {field}="
                        "clock_timestamp()+interval '0.15 seconds' WHERE id=%s",
                        (fence.run_id,),
                    )
                    await asyncio.sleep(0.3)
                    expired = await (
                        await actual_execute(
                            self,
                            f"SELECT {field}<=clock_timestamp() AS expired "
                            "FROM app_state.runs WHERE id=%s",
                            (fence.run_id,),
                        )
                    ).fetchone()
                    injected.append(expired["expired"])
                return await actual_execute(self, sql, params, **kwargs)

            monkeypatch.setattr(psycopg.AsyncConnection, "execute", delayed)
            with pytest.raises(DomainError) as rejected:
                await RunRepository(state).finish(fence, {"summary": "must not publish"})
            assert rejected.value.code == "stale_execution"
            assert injected == [True], (
                "Natural expiry reached without owner replacement or idle timeout"
            )
            current = await RunService(state).get(actor, fence.run_id)
            assert current.state == "RUNNING" and current.report is None
            async with state.connection() as conn:
                messages = await (
                    await conn.execute(
                        "SELECT count(*) AS n FROM app_state.messages "
                        "WHERE run_id=%s AND role='assistant'",
                        (fence.run_id,),
                    )
                ).fetchone()
                events = await (
                    await conn.execute(
                        "SELECT count(*) AS n FROM app_state.events "
                        "WHERE run_id=%s AND kind='run.succeeded'",
                        (fence.run_id,),
                    )
                ).fetchone()
            assert messages["n"] == events["n"] == 0

    run(check())


@pytest.mark.parametrize("field", ["lease_until", "active_deadline", "execution_until"])
def test_evidence_commit_rejects_clock_expiry_after_guard(scenario, monkeypatch, field):
    dep, actors, (conversation, topic, _) = scenario
    actor = actors["A_admin"]
    actual_execute = psycopg.AsyncConnection.execute
    injected = []

    async def check():
        async with systems(dep) as (state, _, gateway):
            fence = await start(state, actor, conversation, topic)
            ticket = await gateway.validate(
                actor,
                fence,
                "SELECT SUM(paid_amount) AS paid_sales FROM analysis.orders WHERE status='paid'",
                {},
            )

            async def delayed(self, sql, params=None, **kwargs):
                if isinstance(sql, str) and "execution_state='DONE'" in sql:
                    table = "query_tickets" if field == "execution_until" else "runs"
                    identity = ticket.query_id if field == "execution_until" else fence.run_id
                    await actual_execute(
                        self,
                        f"UPDATE app_state.{table} SET {field}="
                        "clock_timestamp()+interval '0.15 seconds' WHERE id=%s",
                        (identity,),
                    )
                    await asyncio.sleep(0.3)
                    expired = await (
                        await actual_execute(
                            self,
                            f"SELECT {field}<=clock_timestamp() AS expired "
                            f"FROM app_state.{table} WHERE id=%s",
                            (identity,),
                        )
                    ).fetchone()
                    injected.append(expired["expired"])
                return await actual_execute(self, sql, params, **kwargs)

            monkeypatch.setattr(psycopg.AsyncConnection, "execute", delayed)
            with pytest.raises(DomainError) as rejected:
                await gateway.execute(actor, fence, ticket.query_id)
            assert rejected.value.code == "stale_query_execution"
            assert injected == [True]
            async with state.connection() as conn:
                saved = await (
                    await conn.execute(
                        "SELECT count(*) AS n FROM app_state.query_evidence WHERE run_id=%s",
                        (fence.run_id,),
                    )
                ).fetchone()
                final = await (
                    await conn.execute(
                        "SELECT execution_state FROM app_state.query_tickets WHERE id=%s",
                        (ticket.query_id,),
                    )
                ).fetchone()
            assert saved["n"] == 0 and final["execution_state"] != "DONE"

    run(check())
