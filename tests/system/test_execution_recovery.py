import asyncio
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from insightpilot.admission import Admission
from insightpilot.db import DatabasePools
from insightpilot.errors import DomainError
from insightpilot.runs.confirmations import ConfirmationService, ResumeKey
from insightpilot.runs.models import Scope
from insightpilot.runs.recovery import RecoverySweep
from insightpilot.runs.repository import RunRepository
from insightpilot.runs.service import RunService
from insightpilot.worker import AnalysisWorker
from tests.controlled_provider import ControlledProvider, narrative, plan, query
from tests.integration.test_agent_graph import agent_case as agent_case
from tests.integration.test_mcp_http import mcp_process as mcp_process
from tests.integration.test_run_races import scenario as scenario
from tests.support import run


@asynccontextmanager
async def opened(settings):
    pools = DatabasePools(settings)
    await pools.open()
    try:
        yield pools
    finally:
        await pools.close()


def test_real_redis_atomic_admission_preserves_foreground_slots(agent_case):
    _, _, _, settings = agent_case

    async def check():
        async with opened(settings) as pools:
            admission = Admission(pools.redis, pools.postgres["state"])
            for kind in ("execution", "model"):
                candidates = await asyncio.gather(
                    *(admission.acquire("BACKGROUND", uuid4(), kind=kind) for _ in range(20))
                )
                active = [p for p in candidates if p is not None]
                assert len(active) == 1, "Exactly one background slot across racing callers"
                started = time.monotonic()
                foreground = await admission.acquire("INTERACTIVE", uuid4(), kind=kind)
                assert foreground is not None and time.monotonic() - started <= 1
                assert await admission.acquire("INTERACTIVE", uuid4(), kind=kind) is None
                assert await admission.renew(active[0]) is True
                assert await admission.release(active[0]) is True
                assert await admission.renew(active[0]) is False
                await admission.release(foreground)

    run(check())


def test_worker_runs_actual_graph_then_resumes_original_human_review(agent_case):
    _, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    with ControlledProvider([plan(True), plan(), query(), narrative]) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                state = pools.postgres["state"]
                service = RunService(state)
                receipt = await service.submit(
                    actor, conversation, a, uuid4(), "帮我看销售情况", Scope.BACKGROUND
                )
                worker = AnalysisWorker(configured, pools)
                assert await worker.run_once("BACKGROUND") is True
                row = await service.get(actor, receipt.run_id)
                assert row.state == "WAITING_INPUT" and row.model_used == 1
                review = await ConfirmationService(state, settings.mcp_signing_key).issue(
                    actor, receipt.run_id
                )
                await ConfirmationService(state, settings.mcp_signing_key).resume(
                    actor,
                    receipt.run_id,
                    review["confirmation_id"],
                    {"action": "clarify", "answer": "统计全部已支付订单实付销售额"},
                    ResumeKey(idempotency_key=uuid4(), token=review["token"]),
                )
                assert await AnalysisWorker(configured, pools).run_once("BACKGROUND") is True
                row = await service.get(actor, receipt.run_id)
                assert row.state == "SUCCEEDED" and row.report["claims"]
                assert row.model_used == 4 and row.query_used == 1 and row.attempts == 1
                async with state.connection() as conn:
                    persisted = await (
                        await conn.execute(
                            "SELECT execution_version,owner,lease_until FROM app_state.runs "
                            "WHERE id=%s",
                            (receipt.run_id,),
                        )
                    ).fetchone()
                assert (
                    persisted["execution_version"] == 2
                    and persisted["owner"] is None
                    and persisted["lease_until"] is None
                )

        run(check())
        assert len(provider.requests) == 4


def test_recovery_sweep_materializes_expired_queue_and_confirmation_without_claim(agent_case):
    _, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]

    async def check():
        async with opened(settings) as pools:
            state = pools.postgres["state"]
            service = RunService(state)
            first = await service.submit(
                actor, conversation, a, uuid4(), "queue timeout", Scope.BACKGROUND
            )
            second = await service.submit(
                actor, conversation, b, uuid4(), "waiting timeout", Scope.BACKGROUND
            )
            fence = await RunRepository(state).claim(second.run_id, uuid4())
            await RunRepository(state).pause(fence)
            async with state.connection() as conn:
                await conn.execute(
                    "UPDATE app_state.runs SET queue_until=clock_timestamp()-interval "
                    "'1 second' WHERE id=%s",
                    (first.run_id,),
                )
                await conn.execute(
                    "UPDATE app_state.runs SET "
                    "waiting_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                    (second.run_id,),
                )
            stats = await RecoverySweep(state).run_once()
            assert stats.terminated == 2
            assert (await service.get(actor, first.run_id)).state == "TIMED_OUT"
            assert (await service.get(actor, second.run_id)).state == "TIMED_OUT"

    run(check())


def test_redis_outage_does_not_bypass_admission_or_spend_run_attempt(agent_case):
    _, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]

    async def check():
        async with opened(settings) as pools:
            state = pools.postgres["state"]
            receipt = await RunService(state).submit(
                actor, conversation, a, uuid4(), "统计全部实付销售额", Scope.BACKGROUND
            )
            original = pools.redis
            from redis.asyncio import Redis

            pools.redis = Redis(
                host="127.0.0.1",
                port=1,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
                decode_responses=True,
            )
            try:
                with pytest.raises(DomainError) as error:
                    await AnalysisWorker(settings, pools).run_once("BACKGROUND")
                assert error.value.code == "admission_unavailable"
                row = await RunService(state).get(actor, receipt.run_id)
                assert row.state == "QUEUED" and row.attempts == 0 and row.model_used == 0
            finally:
                await pools.redis.aclose()
                pools.redis = original

    run(check())


def test_two_real_worker_processes_preserve_foreground_capacity_and_background_topic(agent_case):
    import threading

    from tests.worker_process_support import child, result

    _, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    entered = threading.Event()
    release = threading.Event()
    foreground = threading.Event()
    times = {}

    def background_plan(body):
        entered.set()
        assert release.wait(15)
        return plan()

    def foreground_plan(body):
        times["foreground_model"] = time.monotonic()
        foreground.set()
        return plan()

    with ControlledProvider(
        [background_plan, foreground_plan, query(), narrative, query(), narrative]
    ) as provider:

        async def submit():
            async with opened(settings) as pools:
                service = RunService(pools.postgres["state"])
                bg = await service.submit(
                    actor, conversation, a, uuid4(), "统计全部实付销售额", Scope.BACKGROUND
                )
                fg = await service.submit(
                    actor, conversation, b, uuid4(), "统计全部实付销售额", Scope.INTERACTIVE
                )
                return bg, fg

        bg, fg = run(submit())
        with child(settings, provider.url, bg.run_id, "BACKGROUND") as (old, paths, _, __):
            paths["start"].touch()
            assert entered.wait(10), "Background actual model request not reached"
            with child(settings, provider.url, fg.run_id, "INTERACTIVE") as (new, newpaths, _, __):
                times["start"] = time.monotonic()
                newpaths["start"].touch()
                assert foreground.wait(5), "Foreground actual model request not reached"
                assert times["foreground_model"] - times["start"] <= 1
                fresh = result(new, newpaths)
                assert fresh["run"]["state"] == "SUCCEEDED" and fresh["run"]["reports"] == 1
                release.set()
                previous = result(old, paths)
                assert previous["run"]["state"] == "SUCCEEDED" and previous["run"]["reports"] == 1
                assert previous["pid"] != fresh["pid"]
                import json

                (
                    settings.workspace_root
                    / ".cache/insightpilot-verification/worker-capacity-proof.json"
                ).write_text(
                    json.dumps(
                        {
                            "background_pid": old.pid,
                            "foreground_pid": new.pid,
                            "foreground_model_wait_seconds": times["foreground_model"]
                            - times["start"],
                            "both_reports": 2,
                            "actual_model_requests": 6,
                        }
                    ),
                    encoding="utf-8",
                )
        assert len(provider.requests) == 6


@pytest.mark.parametrize("fault_mode", ["suspend", "exit"])
def test_old_process_expired_lease_recovered_after_real_sql_old_checkpoint_zero(
    agent_case, fault_mode
):
    import json

    import psycopg
    from redis import Redis

    from tests.support import dsn
    from tests.worker_process_support import child, result, wait_file

    dep, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    with ControlledProvider([plan(), query(), narrative]) as provider:

        async def submit():
            async with opened(settings) as pools:
                return await RunService(pools.postgres["state"]).submit(
                    actor, conversation, a, uuid4(), "统计全部实付销售额", Scope.BACKGROUND
                )

        receipt = run(submit())
        with child(settings, provider.url, receipt.run_id, "BACKGROUND", fault=True) as (
            old,
            paths,
            pause,
            resume,
        ):
            paths["start"].touch()
            wait_file(paths["fault"], old)
            marker = json.loads(paths["fault"].read_text(encoding="utf-8"))
            if fault_mode == "suspend":
                pause()
            else:
                old.kill()
                old.wait(timeout=5)
            with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
                before = conn.execute(
                    "SELECT query_used,execution_version FROM app_state.runs WHERE id=%s",
                    (receipt.run_id,),
                ).fetchone()
                assert before == (1, 1), (
                    "Fault must occur after a real query and before graph writes"
                )
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM app_state.query_evidence WHERE run_id=%s",
                        (receipt.run_id,),
                    ).fetchone()[0]
                    == 1
                )
                conn.execute(
                    "UPDATE app_state.runs SET lease_until=clock_timestamp()-interval "
                    "'6 seconds' WHERE id=%s",
                    (receipt.run_id,),
                )
            # Inject expiry only for this suspended owner's temporary permits.
            # The test records the hit; it does not pretend 60 wall seconds elapsed.
            with Redis.from_url(
                settings.redis_url.get_secret_value(), decode_responses=True
            ) as redis:
                injected = 0
                for key in redis.scan_iter(match="insightpilot:admission:*"):
                    for member in redis.zrange(key, 0, -1):
                        if member.startswith(marker["owner"] + ":"):
                            redis.zadd(key, {member: 0})
                            injected += 1
                assert injected == 2
            with child(settings, provider.url, receipt.run_id, "BACKGROUND") as (
                new,
                newpaths,
                _,
                __,
            ):
                newpaths["start"].touch()
                fresh = result(new, newpaths)
                assert fresh["run"]["state"] == "SUCCEEDED" and fresh["run"]["reports"] == 1
                assert fresh["run"]["execution_version"] == 2 and fresh["run"]["attempts"] == 2
                assert fresh["run"]["query_used"] == 1 and fresh["run"]["model_used"] == 3
                if fault_mode == "suspend":
                    paths["release"].touch()
                    resume()
                    previous = result(old, paths)
                    assert previous["fault_seen"] and previous["post_fault_attempts"] >= 1
                    assert previous["post_fault_commits"] == 0
                    assert previous["run"]["reports"] == 1 and previous["pid"] != fresh["pid"]
                else:
                    assert old.returncode != 0 and not paths["result"].exists()
                proof = {
                    "fault_mode": fault_mode,
                    "old_pid": old.pid,
                    "new_pid": new.pid,
                    "old_execution_version": marker["execution_version"],
                    "new_execution_version": fresh["run"]["execution_version"],
                    "query_used": fresh["run"]["query_used"],
                    "reports": fresh["run"]["reports"],
                    "post_fault_commits": 0,
                }
                (
                    settings.workspace_root
                    / ".cache/insightpilot-verification"
                    / ("worker-recovery-" + fault_mode + ".json")
                ).write_text(json.dumps(proof), encoding="utf-8")
        assert len(provider.requests) == 3


def test_absolute_worker_deadline_and_cancel_with_no_redis_notification(agent_case):
    import threading

    _, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    entered = threading.Event()
    release = threading.Event()

    def held(body):
        entered.set()
        release.wait(4)
        return plan()

    with ControlledProvider([held]) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                state = pools.postgres["state"]
                service = RunService(state)
                receipt = await service.submit(
                    actor, conversation, a, uuid4(), "统计全部实付销售额", Scope.BACKGROUND
                )
                async with state.connection() as conn:
                    await conn.execute(
                        "UPDATE app_state.runs SET remaining_seconds=1.2 WHERE id=%s",
                        (receipt.run_id,),
                    )
                task = asyncio.create_task(AnalysisWorker(configured, pools).run_once("BACKGROUND"))
                assert await asyncio.to_thread(entered.wait, 3), (
                    "Timeout must hit an actual model request"
                )
                try:
                    assert await asyncio.wait_for(task, 2) is True
                    row = await service.get(actor, receipt.run_id)
                    assert row.state == "TIMED_OUT" and row.report is None and row.query_used == 0
                finally:
                    release.set()

        run(check())
    entered.clear()
    release.clear()
    with ControlledProvider([held]) as provider:

        async def cancelled():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                state = pools.postgres["state"]
                service = RunService(state)
                receipt = await service.submit(
                    actor, conversation, a, uuid4(), "统计全部实付销售额", Scope.BACKGROUND
                )
                task = asyncio.create_task(AnalysisWorker(configured, pools).run_once("BACKGROUND"))
                assert await asyncio.to_thread(entered.wait, 3)
                await service.cancel(actor, receipt.run_id, 1)
                release.set()
                assert await asyncio.wait_for(task, 3) is True
                row = await service.get(actor, receipt.run_id)
                assert row.state == "CANCELLED" and row.report is None and row.query_used == 0

        run(cancelled())


def test_expired_run_is_not_starved_by_a_hundred_unexpired_runs(agent_case):
    _, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]

    async def check():
        async with opened(settings) as pools:
            state = pools.postgres["state"]
            service = RunService(state)
            for index in range(101):
                await service.submit(
                    actor, conversation, a, uuid4(), f"valid queued task {index}", Scope.BACKGROUND
                )
            last = await service.submit(
                actor, conversation, a, uuid4(), "expire newest queued task", Scope.BACKGROUND
            )
            async with state.connection() as conn:
                await conn.execute(
                    "UPDATE app_state.runs SET queue_until=clock_timestamp()-interval "
                    "'1 second' WHERE id=%s",
                    (last.run_id,),
                )
            stats = await RecoverySweep(state).run_once()
            assert (
                stats.terminated == 1
                and (await service.get(actor, last.run_id)).state == "TIMED_OUT"
            )

    run(check())


def test_recovery_does_not_apply_old_answer_to_a_second_clarification(agent_case, monkeypatch):
    _, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    with ControlledProvider([plan(True), plan(True)]) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                state = pools.postgres["state"]
                service = RunService(state)
                receipt = await service.submit(
                    actor, conversation, a, uuid4(), "帮我看销售情况", Scope.BACKGROUND
                )
                worker = AnalysisWorker(configured, pools)
                assert await worker.run_once("BACKGROUND")
                confirmations = ConfirmationService(state, settings.mcp_signing_key)
                first = await confirmations.issue(actor, receipt.run_id)
                await confirmations.resume(
                    actor,
                    receipt.run_id,
                    first["confirmation_id"],
                    {"action": "clarify", "answer": "实付金额，时间还没决定"},
                    ResumeKey(idempotency_key=uuid4(), token=first["token"]),
                )
                original = RunRepository.pause
                hits = []

                async def fail_before_pause(repository, fence, *args, **kwargs):
                    hits.append(True)
                    raise RuntimeError("worker_lost")

                monkeypatch.setattr(RunRepository, "pause", fail_before_pause)
                assert await worker.run_once("BACKGROUND")
                assert hits == [True]
                monkeypatch.setattr(RunRepository, "pause", original)
                async with state.connection() as conn:
                    await conn.execute(
                        "UPDATE app_state.runs SET "
                        "available_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                        (receipt.run_id,),
                    )
                assert await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                row = await service.get(actor, receipt.run_id)
                assert row.state == "WAITING_INPUT" and row.model_used == 2 and row.query_used == 0
                second = await confirmations.issue(actor, receipt.run_id)
                assert second["confirmation_id"] != first["confirmation_id"]

        run(check())
        assert len(provider.requests) == 2


def test_permit_loss_stops_next_model_or_query_and_dataset_disable_blocks_report(agent_case):
    import psycopg
    from redis import Redis

    from tests.support import dsn

    dep, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    hits = []

    def lose_permit(body):
        with Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True) as redis:
            keys = list(redis.scan_iter(match="insightpilot:admission:{execution}:*"))
            assert len(keys) == 2
            redis.delete(*keys)
            hits.append("permit_deleted")
        return plan()

    with ControlledProvider([lose_permit]) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                service = RunService(pools.postgres["state"])
                receipt = await service.submit(
                    actor, conversation, a, uuid4(), "统计全部实付销售额", Scope.BACKGROUND
                )
                assert await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                row = await service.get(actor, receipt.run_id)
                assert (
                    row.state == "QUEUED"
                    and row.error_code == "admission_lost"
                    and row.model_used == 1
                    and row.query_used == 0
                )

        run(check())
        assert hits == ["permit_deleted"] and len(provider.requests) == 1

    # Remove the retrying task before testing dataset visibility independently.
    async def clear():
        async with opened(settings) as pools:
            async with pools.postgres["state"].connection() as conn:
                await conn.execute(
                    "UPDATE app_state.runs SET "
                    "state='CANCELLED',owner=NULL,lease_until=NULL WHERE "
                    "state='QUEUED'"
                )

    run(clear())

    def disable_dataset(body):
        with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
            cursor = conn.execute(
                "UPDATE app_state.dataset_versions SET active=false WHERE tenant_id=%s",
                (actor.tenant_id,),
            )
            assert cursor.rowcount == 1
            hits.append("dataset_disabled")
        return narrative(body)

    try:
        with ControlledProvider([plan(), query(), disable_dataset]) as provider:

            async def check_disabled():
                configured = settings.model_copy(update={"model_base_url": provider.url})
                async with opened(configured) as pools:
                    service = RunService(pools.postgres["state"])
                    receipt = await service.submit(
                        actor, conversation, a, uuid4(), "统计全部实付销售额", Scope.BACKGROUND
                    )
                    assert await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                    row = await service.get(actor, receipt.run_id)
                    assert (
                        row.state == "FAILED"
                        and row.error_code == "dataset_unavailable"
                        and row.report is None
                    )

            run(check_disabled())
            assert len(provider.requests) == 3 and hits[-1] == "dataset_disabled"
    finally:
        with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
            conn.execute(
                "UPDATE app_state.dataset_versions SET active=true WHERE tenant_id=%s",
                (actor.tenant_id,),
            )
