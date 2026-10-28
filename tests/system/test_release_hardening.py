import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from fastapi.testclient import TestClient
from langsmith import Client, tracing_context

from insightpilot.api import create_app
from insightpilot.reports.repository import load_for_run
from insightpilot.runs.service import RunService
from insightpilot.runtime import new_event_loop
from insightpilot.worker import AnalysisWorker
from tests.controlled_provider import ControlledProvider, narrative, plan, query
from tests.integration.test_agent_graph import agent_case as agent_case
from tests.integration.test_mcp_http import mcp_process as mcp_process
from tests.integration.test_run_races import scenario as scenario
from tests.support import dsn, run
from tests.system.test_execution_recovery import opened


def test_partial_usage_never_invents_zero_and_reported_model_is_separate(agent_case):
    _, actors, (conversation, a, _), settings = agent_case
    scripted = [plan(), query(), narrative]
    scripted[0]["_usage"] = {"prompt_tokens": 20}
    with ControlledProvider(scripted) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                receipt = await RunService(pools.postgres["state"]).submit(
                    actors["A_admin"],
                    conversation,
                    a,
                    __import__("uuid").uuid4(),
                    "统计全部实付销售额",
                    "BACKGROUND",
                )
                await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                async with pools.postgres["state"].connection() as conn:
                    row = await (
                        await conn.execute(
                            "SELECT * FROM app_state.model_calls WHERE run_id=%s "
                            "ORDER BY created_at LIMIT 1",
                            (receipt.run_id,),
                        )
                    ).fetchone()
                assert row["input_tokens"] == 20
                assert row["output_tokens"] is None and row["total_tokens"] is None
                assert row["usage_status"] == "partial"
                assert row["provider_model"] == "qwen-plus"

        run(check())


def test_nested_limit_is_visible_to_report_partial_data_guard(agent_case):
    _, actors, (conversation, a, _), settings = agent_case
    sql = (
        "WITH limited AS (SELECT paid_amount FROM analysis.orders "
        "WHERE status='paid' ORDER BY order_id LIMIT 2) "
        "SELECT SUM(paid_amount) AS paid_sales FROM limited"
    )
    with ControlledProvider([plan(), query(sql), narrative]) as provider:

        async def check():
            from insightpilot.agent.graph import build_graph
            from insightpilot.agent.model import AnalysisModel
            from insightpilot.checkpoints import FencedPostgresSaver, checkpoint_scope
            from insightpilot.mcp_client import DataToolsClient
            from tests.integration.test_agent_graph import begin

            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                state = pools.postgres["state"]
                fence, thread = await begin(state, actors["A_admin"], conversation, a)
                model = AnalysisModel(configured, state)
                try:
                    graph = build_graph(
                        model,
                        DataToolsClient(settings, state),
                        FencedPostgresSaver(pools.postgres["checkpoint"]),
                    )
                    with checkpoint_scope(actors["A_admin"], fence, thread):
                        result = await graph.ainvoke(
                            {"question": "统计"},
                            {"configurable": {"thread_id": thread, "checkpoint_ns": ""}},
                        )
                    assert result["status"] == "complete"
                    async with state.connection() as conn, conn.transaction():
                        _, flags, _ = await load_for_run(
                            conn, actors["A_admin"], fence, result["evidence_ids"]
                        )
                    assert all(value["limited"] for value in flags.values())
                    assert result["report"]["warnings"]
                finally:
                    await model.aclose()

        run(check())


def test_readiness_detects_missing_runtime_schema(agent_case):
    dep, _, _, settings = agent_case
    with TestClient(
        create_app(settings), backend_options={"loop_factory": new_event_loop}
    ) as client:
        assert client.get("/api/v1/health/ready").status_code == 200
        with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
            conn.execute("ALTER TABLE app_state.model_calls RENAME TO missing_model_calls")
            try:
                response = client.get("/api/v1/health/ready")
                assert response.status_code == 503
                assert response.json()["error"]["details"]["dependencies"]["state"] is False
            finally:
                conn.execute("ALTER TABLE app_state.missing_model_calls RENAME TO model_calls")


def test_worker_readiness_detects_stopped_loop(agent_case, monkeypatch):
    _, _, _, settings = agent_case

    async def stopped(self):
        return

    monkeypatch.setattr(AnalysisWorker, "serve", stopped)
    configured = settings.model_copy(update={"process_role": "worker"})
    with TestClient(
        create_app(configured), backend_options={"loop_factory": new_event_loop}
    ) as client:
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json()["error"]["details"]["dependencies"]["worker"] is False


def test_worker_disables_inherited_langsmith_export_for_entire_graph(agent_case):
    _, actors, (conversation, a, _), settings = agent_case
    requests = []

    class Collector(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(self.path)
            self.rfile.read(int(self.headers.get("content-length", "0")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_PATCH = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(
        api_url=f"http://127.0.0.1:{server.server_port}",
        api_key="synthetic-tracing-key",
        auto_batch_tracing=False,
    )
    try:
        with ControlledProvider([plan(), query(), narrative]) as provider:

            async def check():
                configured = settings.model_copy(update={"model_base_url": provider.url})
                async with opened(configured) as pools:
                    state = pools.postgres["state"]
                    receipt = await RunService(state).submit(
                        actors["A_admin"],
                        conversation,
                        a,
                        __import__("uuid").uuid4(),
                        "统计全部实付销售额",
                        "BACKGROUND",
                    )
                    with tracing_context(enabled=True, client=client):
                        await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                    assert (
                        await RunService(state).get(actors["A_admin"], receipt.run_id)
                    ).state == "SUCCEEDED"

            run(check())
        client.flush(timeout=5)
        assert requests == [], "Inherited tracing must not export graph business state"
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
