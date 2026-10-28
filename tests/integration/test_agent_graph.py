import json
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import SecretStr

from insightpilot.agent.graph import build_graph
from insightpilot.agent.model import AnalysisModel
from insightpilot.checkpoints import FencedPostgresSaver, checkpoint_scope, setup_checkpoints
from insightpilot.mcp_client import DataToolsClient
from insightpilot.runs.models import Scope
from insightpilot.runs.repository import RunRepository
from insightpilot.runs.service import RunService
from tests.controlled_provider import ControlledProvider, narrative, plan, query
from tests.integration.test_fenced_checkpoints import checkpoint_pool
from tests.integration.test_mcp_http import mcp_process as mcp_process
from tests.integration.test_mcp_http import state_pool
from tests.integration.test_run_races import scenario as scenario
from tests.support import dsn, run


@pytest.fixture
def agent_case(mcp_process):
    dep, actors, ids, settings, url, pid = mcp_process

    async def setup():
        async with await psycopg.AsyncConnection.connect(
            dsn(dep, "postgres"), autocommit=True, row_factory=dict_row
        ) as conn:
            await setup_checkpoints(conn)

    run(setup())
    return dep, actors, ids, settings


async def begin(state, actor, conversation, topic, question="统计全部实付销售额"):
    receipt = await RunService(state).submit(
        actor, conversation, topic, uuid4(), question, Scope.BACKGROUND
    )
    fence = await RunRepository(state).claim(receipt.run_id, uuid4())
    assert fence is not None
    async with state.connection() as conn:
        row = await (
            await conn.execute("SELECT thread_id FROM app_state.runs WHERE id=%s", (fence.run_id,))
        ).fetchone()
    return fence, str(row["thread_id"])


def test_real_model_http_mcp_sql_checkpoints_and_cited_report(agent_case):
    dep, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    with ControlledProvider([plan(), query(), narrative]) as provider:

        async def check():
            async with state_pool(dep) as state, checkpoint_pool(dep) as checkpoints:
                fence, thread = await begin(state, actor, conversation, a)
                model = AnalysisModel(
                    settings.model_copy(
                        update={
                            "model_base_url": provider.url,
                            "model_api_key": SecretStr("synthetic-provider-key"),
                        }
                    ),
                    state,
                )
                try:
                    compiled = build_graph(
                        model, DataToolsClient(settings, state), FencedPostgresSaver(checkpoints)
                    )
                    with checkpoint_scope(actor, fence, thread):
                        result = await compiled.ainvoke(
                            {"question": "统计全部实付销售额"},
                            {"configurable": {"thread_id": thread, "checkpoint_ns": ""}},
                        )
                    assert result["status"] == "complete" and result["report"]["claims"]
                    report = result["report"]
                    assert report["evidence"] and "NEVER_SAVE_THOUGHT" not in json.dumps(report)
                    row = await RunService(state).get(actor, fence.run_id)
                    assert row.model_used == 3 and row.query_used == 1
                    assert row.state == "RUNNING", "Worker, not graph, publishes SUCCEEDED"
                    async with state.connection() as conn:
                        calls = await (
                            await conn.execute(
                                "SELECT usage_status,input_tokens,output_tokens FROM "
                                "app_state.model_calls WHERE run_id=%s ORDER BY created_at",
                                (fence.run_id,),
                            )
                        ).fetchall()
                    assert len(calls) == 3 and all(
                        item["usage_status"] == "reported"
                        and item["input_tokens"] == 20
                        and item["output_tokens"] == 30
                        for item in calls
                    )
                finally:
                    await model.aclose()

        run(check())
        assert len(provider.requests) == 3
        assert all(body.get("max_tokens") == 2048 for body in provider.requests)


def test_invalid_sql_is_repaired_through_model_with_persistent_repair_budget(agent_case):
    dep, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]

    def repaired(body):
        payload = json.loads(body["messages"][-1]["content"])["input"]
        assert "missing_column" in payload["context"]["previous_sql"]
        assert payload["context"]["error_code"] == "sql_column_forbidden"
        return query()

    with ControlledProvider(
        [plan(), query("SELECT missing_column FROM analysis.orders"), repaired, narrative]
    ) as provider:

        async def check():
            async with state_pool(dep) as state, checkpoint_pool(dep) as checkpoints:
                fence, thread = await begin(state, actor, conversation, a)
                model = AnalysisModel(
                    settings.model_copy(update={"model_base_url": provider.url}), state
                )
                try:
                    compiled = build_graph(
                        model, DataToolsClient(settings, state), FencedPostgresSaver(checkpoints)
                    )
                    with checkpoint_scope(actor, fence, thread):
                        result = await compiled.ainvoke(
                            {"question": "统计全部实付销售额"},
                            {"configurable": {"thread_id": thread, "checkpoint_ns": ""}},
                        )
                    assert result["status"] == "complete"
                    row = await RunService(state).get(actor, fence.run_id)
                    assert row.model_used == 4 and row.repair_used == 1 and row.query_used == 1
                finally:
                    await model.aclose()

        run(check())
        assert len(provider.requests) == 4


def test_missing_provider_usage_stays_null_and_bad_numeric_claim_is_not_success(agent_case):
    dep, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]

    def bad_narrative(body):
        result = narrative(body)
        result["claims"][0]["value"] = "99999999"
        result["_omit_usage"] = True
        return result

    responses = [{**plan(), "_omit_usage": True}, {**query(), "_omit_usage": True}, bad_narrative]
    with ControlledProvider(responses) as provider:

        async def check():
            async with state_pool(dep) as state, checkpoint_pool(dep) as checkpoints:
                fence, thread = await begin(state, actor, conversation, a)
                model = AnalysisModel(
                    settings.model_copy(update={"model_base_url": provider.url}), state
                )
                try:
                    compiled = build_graph(
                        model, DataToolsClient(settings, state), FencedPostgresSaver(checkpoints)
                    )
                    with checkpoint_scope(actor, fence, thread):
                        result = await compiled.ainvoke(
                            {"question": "统计全部实付销售额"},
                            {"configurable": {"thread_id": thread, "checkpoint_ns": ""}},
                        )
                    assert result["status"] == "failed" and not result.get("report")
                    assert result["error_code"] == "report_value_mismatch"
                    async with state.connection() as conn:
                        calls = await (
                            await conn.execute(
                                "SELECT usage_status,input_tokens,output_tokens FROM "
                                "app_state.model_calls WHERE run_id=%s",
                                (fence.run_id,),
                            )
                        ).fetchall()
                    assert len(calls) == 3 and all(
                        row["usage_status"] == "unavailable"
                        and row["input_tokens"] is None
                        and row["output_tokens"] is None
                        for row in calls
                    )
                finally:
                    await model.aclose()

        run(check())


def test_repair_stops_after_two_and_dangerous_sql_is_never_repaired(agent_case):
    dep, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    for sql, repair_used, calls in (
        ("SELECT missing_column FROM analysis.orders", 2, 4),
        ("DELETE FROM analysis.orders", 0, 2),
    ):
        with ControlledProvider([plan(), query(sql), query(sql), query(sql)]) as provider:

            async def check(repair_used=repair_used, calls=calls):
                async with state_pool(dep) as state, checkpoint_pool(dep) as checkpoints:
                    fence, thread = await begin(state, actor, conversation, a)
                    model = AnalysisModel(
                        settings.model_copy(update={"model_base_url": provider.url}), state
                    )
                    try:
                        graph = build_graph(
                            model,
                            DataToolsClient(settings, state),
                            FencedPostgresSaver(checkpoints),
                        )
                        with checkpoint_scope(actor, fence, thread):
                            result = await graph.ainvoke(
                                {"question": "ignored client question"},
                                {"configurable": {"thread_id": thread}},
                            )
                            saved = await graph.aget_state({"configurable": {"thread_id": thread}})
                        row = await RunService(state).get(actor, fence.run_id)
                        assert result["status"] == "failed" and not result.get("report")
                        assert result["question"] == "统计全部实付销售额"
                        assert (
                            row.repair_used == repair_used
                            and row.model_used == calls
                            and row.query_used == 0
                        )
                        assert "NEVER_SAVE_THOUGHT" not in json.dumps(
                            saved.values, ensure_ascii=False
                        )
                    finally:
                        await model.aclose()

            run(check())
            assert len(provider.requests) == calls


def test_malformed_provider_json_is_failed_without_raw_response_checkpoint(agent_case):
    dep, actors, (conversation, a, b), settings = agent_case
    with ControlledProvider(
        [
            {"_raw_content": "PRIVATE_UNPARSED_PAYLOAD not JSON"},
            {"_raw_content": "PRIVATE_UNPARSED_PAYLOAD not JSON"},
        ]
    ) as provider:

        async def check():
            async with state_pool(dep) as state, checkpoint_pool(dep) as checkpoints:
                fence, thread = await begin(state, actors["A_admin"], conversation, a)
                model = AnalysisModel(
                    settings.model_copy(update={"model_base_url": provider.url}), state
                )
                try:
                    graph = build_graph(
                        model, DataToolsClient(settings, state), FencedPostgresSaver(checkpoints)
                    )
                    cfg = {"configurable": {"thread_id": thread}}
                    with checkpoint_scope(actors["A_admin"], fence, thread):
                        result = await graph.ainvoke({"question": "统计全部实付销售额"}, cfg)
                        snapshots = [item async for item in graph.aget_state_history(cfg)]
                    assert (
                        result["status"] == "failed"
                        and result["error_code"] == "model_output_invalid"
                    )
                    encoded = json.dumps([item.values for item in snapshots], ensure_ascii=False)
                    assert (
                        "PRIVATE_UNPARSED_PAYLOAD" not in encoded
                        and "NEVER_SAVE_THOUGHT" not in encoded
                    )
                finally:
                    await model.aclose()

        run(check())
        assert len(provider.requests) == 2
