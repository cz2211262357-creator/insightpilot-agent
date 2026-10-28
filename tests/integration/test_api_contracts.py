import json
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from insightpilot.api import create_app
from insightpilot.errors import DomainError
from insightpilot.runs.events import EventReader
from insightpilot.runs.models import Scope
from insightpilot.runs.service import RunService
from insightpilot.runtime import new_event_loop
from insightpilot.worker import AnalysisWorker
from tests.controlled_provider import ControlledProvider, narrative, plan, query
from tests.integration.test_agent_graph import agent_case as agent_case
from tests.integration.test_mcp_http import mcp_process as mcp_process
from tests.integration.test_run_races import scenario as scenario
from tests.support import PASSWORD, dsn, run
from tests.system.test_execution_recovery import opened


def login(http, actor, email="admin@a.synthetic.invalid"):
    result = http.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(actor.tenant_id), "email": email, "password": PASSWORD},
    )
    assert result.status_code == 200
    return {"x-csrf-token": http.cookies.get("ip_csrf")}


def test_browser_catalog_members_and_schema_contracts(agent_case):
    _, actors, _, settings = agent_case
    app = create_app(settings)
    with TestClient(
        app,
        backend_options={"loop_factory": new_event_loop},
        headers={"origin": "http://localhost:8090"},
    ) as http:
        assert http.get("/api/v1/datasets").status_code == 401
        login(http, actors["A_admin"])
        catalog = http.get("/api/v1/datasets")
        assert catalog.status_code == 200 and len(catalog.json()["views"]) == 6
        metrics = http.get("/api/v1/metrics").json()
        assert len(metrics) == 6 and any(item["metric_id"] == "paid_sales" for item in metrics)
        members = http.get("/api/v1/members")
        assert members.status_code == 200 and all(
            member["role"] in ("admin", "analyst") for member in members.json()
        )
        system = http.get("/api/v1/system").json()
        assert system["model_name"] == "qwen-plus" and system["data_kind"] == "synthetic"
        assert "key" not in json.dumps(system)
        schema = http.get("/openapi.json").json()
        for path, method, status in (
            ("/api/v1/runs/{run_id}", "get", "200"),
            ("/api/v1/runs/{run_id}/resume", "post", "202"),
            ("/api/v1/runs/{run_id}/report", "get", "200"),
            ("/api/v1/datasets", "get", "200"),
        ):
            response = schema["paths"][path][method]["responses"][status]["content"][
                "application/json"
            ]["schema"]
            assert response.get("$ref"), (
                "Successful API responses must generate concrete TypeScript types"
            )
        assert schema["paths"]["/api/v1/runs/{run_id}/events"]["get"]["responses"]["200"][
            "content"
        ].get("text/event-stream")


def test_completed_report_evidence_history_are_currently_authorized(agent_case):
    dep, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    with ControlledProvider([plan(), query(), narrative]) as provider:

        async def complete():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                receipt = await RunService(pools.postgres["state"]).submit(
                    actor, conversation, a, uuid4(), "统计全部实付销售额", Scope.BACKGROUND
                )
                assert await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                return str(receipt.run_id)

        run_id = run(complete())
    with TestClient(
        create_app(settings),
        backend_options={"loop_factory": new_event_loop},
        headers={"origin": "http://localhost:8090"},
    ) as http:
        login(http, actor)
        report = http.get(f"/api/v1/runs/{run_id}/report")
        assert report.status_code == 200 and report.json()["claims"]
        identity = report.json()["evidence"][0]["evidence_id"]
        evidence = http.get(f"/api/v1/runs/{run_id}/evidence/{identity}")
        assert evidence.status_code == 200
        assert "SELECT" in evidence.json()["sql"].upper() and evidence.json()["metric_definitions"]
        assert evidence.json()["evidence"]["sha256"] == report.json()["evidence"][0]["sha256"]
        listed = http.get("/api/v1/runs?limit=1")
        assert listed.status_code == 200 and listed.json()["items"][0]["has_report"]
        assert listed.json()["items"][0]["report"] is None
        snapshot = http.get(f"/api/v1/runs/{run_id}").json()
        assert snapshot["snapshot_seq"] > 0 and snapshot["execution_version"] == 1
        assert snapshot["question"] == "统计全部实付销售额"
        with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
            conn.execute(
                "UPDATE app_state.dataset_versions SET active=false WHERE tenant_id=%s",
                (actor.tenant_id,),
            )
        try:
            assert http.get(f"/api/v1/runs/{run_id}/report").status_code == 403
            assert http.get(f"/api/v1/runs/{run_id}/evidence/{identity}").status_code == 403
            hidden = http.get(f"/api/v1/runs/{run_id}").json()
            assert hidden["report"] is None and hidden["report_error_code"] == "dataset_unavailable"
            history = http.get(f"/api/v1/topics/{a}/history").json()
            assert history["runs"][0]["report"] is None
            assert all(
                message["content"] != report.json()["summary"]
                for message in history["messages"]
                if message["role"] == "assistant"
            ), "Hidden reports must not leak through assistant history"
        finally:
            with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
                conn.execute(
                    "UPDATE app_state.dataset_versions SET active=true WHERE tenant_id=%s",
                    (actor.tenant_id,),
                )
        old = http.cookies.get("ip_session")
        login(http, actors["B_admin"], "admin@b.synthetic.invalid")
        assert http.get(f"/api/v1/runs/{run_id}/report").status_code == 404

        async def old_rejected():
            async with opened(settings) as pools:
                with pytest.raises(DomainError) as error:
                    await EventReader(pools.postgres["state"]).read(old, run_id)
                assert error.value.status == 401

        run(old_rejected())


def test_durable_event_cursor_sse_projection_and_logout_reauthentication(agent_case):
    _, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]

    async def queued():
        async with opened(settings) as pools:
            receipt = await RunService(pools.postgres["state"]).submit(
                actor, conversation, a, uuid4(), "统计全部实付销售额", Scope.BACKGROUND
            )
            return str(receipt.run_id)

    run_id = run(queued())
    with TestClient(
        create_app(settings),
        backend_options={"loop_factory": new_event_loop},
        headers={"origin": "http://localhost:8090"},
    ) as http:
        headers = login(http, actor)
        cookie = http.cookies.get("ip_session")

        async def batch():
            async with opened(settings) as pools:
                first = await EventReader(pools.postgres["state"]).read(cookie, run_id, 0)
                assert len(first.events) == 1
                event = first.events[0].model_dump(mode="json")
                assert {
                    "seq",
                    "run_id",
                    "topic_id",
                    "task_revision",
                    "execution_version",
                    "scope",
                    "epoch",
                } <= event.keys()
                assert event["scope"] == "BACKGROUND" and event["execution_version"] == 0
                second = await EventReader(pools.postgres["state"]).read(
                    cookie, run_id, first.cursor
                )
                assert second.events == [] and second.cursor == first.cursor
                return event

        event = run(batch())
        stream = http.get(f"/api/v1/runs/{run_id}/events?once=true&cursor=0")
        assert stream.status_code == 200 and stream.headers["content-type"].startswith(
            "text/event-stream"
        )
        assert "event: run_event" in stream.text and f"id: {event['seq']}" in stream.text
        assert "reasoning_content" not in stream.text
        assert http.post("/api/v1/auth/logout", headers=headers).status_code == 204

        async def revoked():
            async with opened(settings) as pools:
                with pytest.raises(DomainError) as error:
                    await EventReader(pools.postgres["state"]).read(cookie, run_id, event["seq"])
                assert error.value.status == 401

        run(revoked())


def test_run_snapshot_state_and_sequence_are_one_database_snapshot(agent_case, monkeypatch):
    import insightpilot.reports.history as history_module
    from insightpilot.runs.repository import RunRepository

    _, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]

    async def check():
        async with opened(settings) as pools:
            state = pools.postgres["state"]
            service = RunService(state)
            receipt = await service.submit(
                actor, conversation, a, uuid4(), "snapshot race fixture", Scope.BACKGROUND
            )
            async with state.connection() as conn:
                before = (
                    await (
                        await conn.execute(
                            "SELECT MAX(id) AS seq FROM app_state.events WHERE run_id=%s",
                            (receipt.run_id,),
                        )
                    ).fetchone()
                )["seq"]
            original = history_module.visible_snapshot
            hits = []

            async def concurrent_finish(conn, current, row):
                if not hits:
                    hits.append(True)
                    fence = await RunRepository(state).claim(receipt.run_id, uuid4())
                    await RunRepository(state).finish(
                        fence,
                        {
                            "summary": "synthetic snapshot boundary",
                            "claims": [],
                            "evidence": [],
                            "charts": [],
                            "warnings": [],
                            "catalog_version": "2026-10-v1",
                        },
                    )
                return await original(conn, current, row)

            monkeypatch.setattr(history_module, "visible_snapshot", concurrent_finish)
            snapshot = await service.get(actor, receipt.run_id)
            assert hits == [True]
            assert snapshot.state == "QUEUED" and snapshot.snapshot_seq == before, (
                "A queued state cannot carry a later succeeded event sequence"
            )

    run(check())


def test_delayed_new_message_cannot_activate_an_old_topic_but_original_retry_is_immutable(
    agent_case,
):
    _, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]

    async def check():
        async with opened(settings) as pools:
            service = RunService(pools.postgres["state"])
            key = uuid4()
            first = await service.submit(
                actor, conversation, a, key, "original", Scope.BACKGROUND, expected_epoch=0
            )
            switched = await service.switch_topic(actor, conversation, b, first.epoch)
            retry = await service.submit(
                actor, conversation, a, key, "original", Scope.BACKGROUND, expected_epoch=0
            )
            assert retry == first
            with pytest.raises(DomainError) as error:
                await service.submit(
                    actor,
                    conversation,
                    a,
                    uuid4(),
                    "late first delivery",
                    Scope.BACKGROUND,
                    expected_epoch=0,
                )
            assert error.value.code == "conversation_conflict"
            current = await service.conversation(actor, conversation)
            assert current["active_topic_id"] == b and current["epoch"] == switched.epoch

    run(check())
