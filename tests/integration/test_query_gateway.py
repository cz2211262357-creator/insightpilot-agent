import time
from contextlib import asynccontextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from insightpilot.errors import DomainError
from insightpilot.query.gateway import MAX_RESULT_BYTES, QueryGateway
from insightpilot.runs.models import Scope
from insightpilot.runs.repository import RunRepository
from insightpilot.runs.service import RunService
from tests.integration.test_run_races import scenario as scenario
from tests.support import dsn, make_conversation, run


@asynccontextmanager
async def systems(dep):
    async with AsyncConnectionPool(
        dsn(dep),
        open=False,
        min_size=0,
        max_size=6,
        kwargs={"autocommit": True, "row_factory": dict_row},
    ) as state:
        async with AsyncConnectionPool(
            dsn(dep, "ip_reader"),
            open=False,
            min_size=1,
            max_size=1,
            kwargs={"autocommit": True, "row_factory": dict_row},
        ) as reader:
            yield (
                state,
                reader,
                QueryGateway(
                    state, reader, SecretStr("synthetic-confirmation-key-with-at-least-32-bytes")
                ),
            )


async def start(state, actor, conversation, topic):
    receipt = await RunService(state).submit(
        actor, conversation, topic, uuid4(), "Synthetic query acceptance", Scope.BACKGROUND
    )
    fence = await RunRepository(state).claim(receipt.run_id, uuid4())
    assert fence is not None
    return fence


async def approve_for_test(dep, state, gateway, actor, fence, ticket):
    # Task8 replaces this explicit fixture digest with an actual official checkpoint.
    with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
        admin.execute(
            "UPDATE app_state.runs SET checkpoint_digest=%s WHERE id=%s",
            ("synthetic-checkpoint-fixture", fence.run_id),
        )
    await RunRepository(state).pause(fence, query_id=ticket.query_id)
    request = await gateway.create_confirmation(actor, fence.run_id, ticket.query_id)
    receipt = await gateway.approve(actor, request["confirmation_id"], request["token"])
    assert receipt["accepted"] is True
    assert await gateway.approve(actor, request["confirmation_id"], request["token"]) == receipt
    resumed = await RunRepository(state).claim(fence.run_id, uuid4())
    assert resumed is not None
    return resumed, request["confirmation_id"]


def test_actual_decimal_empty_zero_denominator_and_duplicate_execution(scenario):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, reader, gateway):
            fence = await start(state, actor, conversation, a)
            ticket = await gateway.validate(
                actor,
                fence,
                "SELECT SUM(paid_amount) AS paid_sales FROM analysis.orders WHERE status='paid'",
                {},
            )
            assert ticket.requires_approval is False
            result = await gateway.execute(actor, fence, ticket.query_id)
            assert result.count == 1 and result.rows[0]["paid_sales"].endswith(
                ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")
            )
            assert isinstance(result.rows[0]["paid_sales"], str) and len(result.sha256) == 64
            assert await gateway.execute(actor, fence, ticket.query_id) == result
            empty = await gateway.validate(
                actor,
                fence,
                "SELECT SUM(paid_amount) AS paid_sales FROM analysis.orders WHERE order_id<0",
                {},
            )
            assert (await gateway.execute(actor, fence, empty.query_id)).rows == [
                {"paid_sales": None}
            ]
            zero = await gateway.validate(
                actor,
                fence,
                "SELECT COUNT(*)::numeric/NULLIF(COUNT(*),0) AS aftersale_rate "
                "FROM analysis.orders WHERE order_id<0",
                {},
            )
            assert (await gateway.execute(actor, fence, zero.query_id)).rows == [
                {"aftersale_rate": None}
            ]
            amount = await gateway.validate(
                actor, fence, "SELECT paid_amount FROM analysis.orders WHERE order_id=7", {}
            )
            assert (await gateway.execute(actor, fence, amount.query_id)).rows == [
                {"paid_amount": "0.00"}
            ]
            assert (await RunService(state).get(actor, fence.run_id)).query_used == 4

    run(check())


def test_single_reader_pool_two_tenants_and_context_reset(scenario):
    dep, actors, (conversation, a, b) = scenario
    conversation_b, topic_b, _ = make_conversation(dep, actors["B_admin"])

    async def check():
        async with systems(dep) as (state, reader, gateway):
            for actor, conv, topic, label in [
                (actors["A_admin"], conversation, a, "SYN-A-"),
                (actors["B_admin"], conversation_b, topic_b, "SYN-B-"),
            ]:
                fence = await start(state, actor, conv, topic)
                ticket = await gateway.validate(
                    actor,
                    fence,
                    "SELECT customer_ref FROM analysis.orders ORDER BY order_id LIMIT 5",
                    {},
                )
                result = await gateway.execute(actor, fence, ticket.query_id)
                assert result.count == 5 and all(
                    row["customer_ref"].startswith(label) for row in result.rows
                )
            async with reader.connection() as conn:
                assert await (await conn.execute("SELECT * FROM analysis.orders")).fetchall() == []

    run(check())


@pytest.mark.parametrize("wide", [False, True])
def test_result_row_and_byte_caps_are_explicit_not_fake_full_aggregate(scenario, wide):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, reader, gateway):
            fence = await start(state, actor, conversation, a)
            extra = (", '" + "虚" * 1500 + "' AS payload") if wide else ""
            sql = (
                "SELECT a.order_id AS a_order,b.order_id AS b_order"
                + extra
                + (
                    " FROM analysis.orders a CROSS JOIN analysis.orders b ORDER BY "
                    "a.order_id,b.order_id"
                )
            )
            ticket = await gateway.validate(actor, fence, sql, {})
            assert ticket.requires_approval is False
            result = await gateway.execute(actor, fence, ticket.query_id)
            assert result.truncated is True
            assert 0 < result.count < 1000 if wide else result.count == 1000
            assert len(result.model_dump_json().encode("utf-8")) <= MAX_RESULT_BYTES

    run(check())


def test_explain_real_cost_and_row_gates_and_no_implicit_approval(scenario):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, reader, gateway):
            fence = await start(state, actor, conversation, a)
            rows = await gateway.validate(
                actor,
                fence,
                "SELECT a.order_id AS a_order,b.order_id AS b_order,c.order_id AS "
                "c_order FROM analysis.orders a CROSS JOIN analysis.orders b "
                "CROSS JOIN analysis.orders c",
                {},
            )
            assert rows.requires_approval and rows.explain["estimated_rows"] > 100000
            costly = await gateway.validate(
                actor,
                fence,
                "SELECT COUNT(*) AS n FROM analysis.orders a CROSS JOIN "
                "analysis.orders b CROSS JOIN analysis.orders c CROSS JOIN "
                "analysis.orders d",
                {},
            )
            assert costly.requires_approval and costly.explain["total_cost"] > 10000
            with pytest.raises(DomainError) as denied:
                await gateway.execute(actor, fence, costly.query_id)
            assert denied.value.code == "query_approval_required"
            assert (await RunService(state).get(actor, fence.run_id)).query_used == 0

    run(check())


def test_approved_query_still_has_real_three_second_timeout(scenario):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, reader, gateway):
            fence = await start(state, actor, conversation, a)
            ticket = await gateway.validate(
                actor,
                fence,
                "SELECT COUNT(*) AS n FROM analysis.orders a CROSS JOIN "
                "analysis.orders b CROSS JOIN analysis.orders c CROSS JOIN "
                "analysis.orders d CROSS JOIN analysis.orders e CROSS JOIN "
                "analysis.orders f",
                {},
            )
            assert ticket.requires_approval
            resumed, approval_id = await approve_for_test(dep, state, gateway, actor, fence, ticket)
            started = time.monotonic()
            with pytest.raises(DomainError) as stopped:
                await gateway.execute(actor, resumed, ticket.query_id, approval_id)
            assert stopped.value.code == "sql_timeout"
            assert 2.5 < time.monotonic() - started < 5
            async with state.connection() as conn:
                assert (
                    await (
                        await conn.execute(
                            "SELECT COUNT(*) AS n FROM app_state.query_evidence WHERE run_id=%s",
                            (fence.run_id,),
                        )
                    ).fetchone()
                )["n"] == 0

    run(check())


@pytest.mark.parametrize("action", ["revoke", "cancel"])
def test_revoked_or_cancelled_after_actual_sql_never_returns_or_saves_result(
    scenario, monkeypatch, action
):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, reader, gateway):
            fence = await start(state, actor, conversation, a)
            ticket = await gateway.validate(
                actor, fence, "SELECT SUM(paid_amount) AS paid_sales FROM analysis.orders", {}
            )
            original = gateway._fetch

            async def boundary(*args, **kwargs):
                result = await original(*args, **kwargs)
                if action == "revoke":
                    with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                        admin.execute(
                            "UPDATE app_state.users SET enabled=false WHERE id=%s", (actor.user_id,)
                        )
                else:
                    await RunService(state).cancel(actor, fence.run_id, 1)
                return result

            monkeypatch.setattr(gateway, "_fetch", boundary)
            with pytest.raises(DomainError):
                await gateway.execute(actor, fence, ticket.query_id)
            with psycopg.connect(dsn(dep, "postgres"), row_factory=dict_row) as admin:
                assert (
                    admin.execute(
                        "SELECT COUNT(*) AS n FROM app_state.query_evidence WHERE run_id=%s",
                        (fence.run_id,),
                    ).fetchone()["n"]
                    == 0
                )

    run(check())


def test_ticket_cannot_cross_run_or_revision(scenario):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, reader, gateway):
            fence = await start(state, actor, conversation, a)
            ticket = await gateway.validate(
                actor, fence, "SELECT order_id FROM analysis.orders LIMIT 1", {}
            )
            revised = await RunService(state).revise(
                actor, fence.run_id, 1, uuid4(), "修订后的报告"
            )
            new = await RunRepository(state).claim(revised.run_id, uuid4())
            assert new is not None
            with pytest.raises(DomainError):
                await gateway.execute(actor, new, ticket.query_id)

    run(check())


def test_cached_evidence_does_not_bypass_current_catalog_version(scenario, monkeypatch):
    from insightpilot.query import repository

    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, reader, gateway):
            fence = await start(state, actor, conversation, a)
            ticket = await gateway.validate(
                actor, fence, "SELECT COUNT(*) AS order_count FROM analysis.orders", {}
            )
            result = await gateway.execute(actor, fence, ticket.query_id)
            assert result.count == 1
            manifest = repository.load_manifest()
            monkeypatch.setattr(
                repository,
                "load_manifest",
                lambda: {**manifest, "version": "changed-after-execution"},
            )
            with pytest.raises(DomainError) as stale:
                await gateway.execute(actor, fence, ticket.query_id)
            assert stale.value.code == "query_stale"

    run(check())


def test_dataset_as_of_covers_every_synthetic_business_event(scenario):
    dep, _, _ = scenario
    with psycopg.connect(dsn(dep, "postgres"), row_factory=dict_row) as admin:
        rows = admin.execute("""
            SELECT COUNT(*) AS invalid FROM (
              SELECT tenant_id,dataset_version,created_at AS occurred FROM business_private.orders
              UNION ALL SELECT tenant_id,dataset_version,paid_at FROM business_private.orders
              UNION ALL SELECT tenant_id,dataset_version,created_at
                FROM business_private.aftersales
              UNION ALL SELECT tenant_id,dataset_version,resolved_at
                FROM business_private.aftersales
            ) x JOIN app_state.dataset_versions d
              ON d.tenant_id=x.tenant_id AND d.id=x.dataset_version
            WHERE x.occurred>d.as_of
            """).fetchone()
        assert rows["invalid"] == 0, "dataset as_of must not predate captured paid/refund events"


@pytest.mark.parametrize("change", ["checkpoint", "budget", "revision", "expiry"])
def test_confirmation_rejects_changed_snapshot_and_never_resumes(scenario, change):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, reader, gateway):
            fence = await start(state, actor, conversation, a)
            ticket = await gateway.validate(
                actor,
                fence,
                "SELECT a.order_id AS a_order,b.order_id AS b_order,c.order_id AS "
                "c_order FROM analysis.orders a CROSS JOIN analysis.orders b "
                "CROSS JOIN analysis.orders c",
                {},
            )
            assert ticket.requires_approval
            with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                admin.execute(
                    "UPDATE app_state.runs SET checkpoint_digest=%s WHERE id=%s",
                    ("synthetic-checkpoint-fixture", fence.run_id),
                )
            await RunRepository(state).pause(fence, query_id=ticket.query_id)
            request = await gateway.create_confirmation(actor, fence.run_id, ticket.query_id)
            if change == "revision":
                await RunService(state).revise(actor, fence.run_id, 1, uuid4(), "新的独立报告")
            else:
                with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                    if change == "checkpoint":
                        admin.execute(
                            "UPDATE app_state.runs SET checkpoint_digest='changed' WHERE id=%s",
                            (fence.run_id,),
                        )
                    if change == "budget":
                        admin.execute(
                            "UPDATE app_state.runs SET query_used=query_used+1 WHERE id=%s",
                            (fence.run_id,),
                        )
                    if change == "expiry":
                        admin.execute(
                            "UPDATE app_state.confirmations SET "
                            "expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                            (request["confirmation_id"],),
                        )
            with pytest.raises(DomainError):
                await gateway.approve(actor, request["confirmation_id"], request["token"])
            status = await RunService(state).get(actor, fence.run_id)
            assert status.state == ("SUPERSEDED" if change == "revision" else "WAITING_INPUT")

    run(check())


def test_inflight_duplicate_does_not_spend_a_second_query_reservation(scenario, monkeypatch):
    import asyncio

    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, reader, gateway):
            fence = await start(state, actor, conversation, a)
            ticket = await gateway.validate(
                actor, fence, "SELECT COUNT(*) AS order_count FROM analysis.orders", {}
            )
            entered = asyncio.Event()
            release = asyncio.Event()
            original = gateway._fetch

            async def hold(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            monkeypatch.setattr(gateway, "_fetch", hold)
            first = asyncio.create_task(gateway.execute(actor, fence, ticket.query_id))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                with pytest.raises(DomainError) as pending:
                    await gateway.execute(actor, fence, ticket.query_id)
                assert pending.value.code == "query_in_progress"
                assert (await RunService(state).get(actor, fence.run_id)).query_used == 1
            finally:
                release.set()
            result = await first
            assert await gateway.execute(actor, fence, ticket.query_id) == result

    run(check())


def test_a_waiting_report_b_text_confirm_does_not_resume_a_but_explicit_bound_approval_does(
    scenario,
):
    dep, actors, (conversation, a, b) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, reader, gateway):
            fence = await start(state, actor, conversation, a)
            ticket = await gateway.validate(
                actor,
                fence,
                "SELECT a.order_id AS a_order,b.order_id AS b_order,c.order_id AS "
                "c_order FROM analysis.orders a CROSS JOIN analysis.orders b "
                "CROSS JOIN analysis.orders c",
                {},
            )
            assert ticket.requires_approval
            with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                admin.execute(
                    "UPDATE app_state.runs SET checkpoint_digest=%s WHERE id=%s",
                    ("synthetic-checkpoint-fixture", fence.run_id),
                )
            await RunRepository(state).pause(fence, query_id=ticket.query_id)
            request = await gateway.create_confirmation(actor, fence.run_id, ticket.query_id)
            await RunService(state).submit(
                actor, conversation, b, uuid4(), "确认", Scope.BACKGROUND
            )
            assert (await RunService(state).get(actor, fence.run_id)).state == "WAITING_INPUT"
            with pytest.raises(DomainError):
                await gateway.approve(
                    actors["A_other"], request["confirmation_id"], request["token"]
                )
            with pytest.raises(DomainError):
                await gateway.approve(actor, request["confirmation_id"], "forged")
            accepted = await gateway.approve(actor, request["confirmation_id"], request["token"])
            assert accepted["accepted"]
            new = await RunRepository(state).claim(fence.run_id, uuid4())
            assert new is not None
            result = await gateway.execute(actor, new, ticket.query_id, request["confirmation_id"])
            assert result.truncated and result.count == 1000
            assert (await RunService(state).conversation(actor, conversation))[
                "active_topic_id"
            ] == b

    run(check())
