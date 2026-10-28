from uuid import UUID, uuid4

from langgraph.types import Command

from insightpilot.agent.graph import build_graph
from insightpilot.agent.model import AnalysisModel
from insightpilot.checkpoints import FencedPostgresSaver, checkpoint_scope
from insightpilot.mcp_client import DataToolsClient
from insightpilot.runs.confirmations import ConfirmationService, ResumeKey
from insightpilot.runs.models import Scope
from insightpilot.runs.repository import RunRepository
from insightpilot.runs.service import RunService
from tests.controlled_provider import ControlledProvider, narrative, plan, query
from tests.integration.test_agent_graph import agent_case as agent_case
from tests.integration.test_agent_graph import begin
from tests.integration.test_fenced_checkpoints import checkpoint_pool
from tests.integration.test_mcp_http import mcp_process as mcp_process
from tests.integration.test_mcp_http import state_pool
from tests.integration.test_run_races import scenario as scenario
from tests.support import run


def test_clarification_interrupt_explicit_resume_preserves_budget_and_chat_does_not_approve(
    agent_case,
):
    dep, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    with ControlledProvider([plan(True), plan(), query(), narrative]) as provider:

        async def check():
            async with state_pool(dep) as state, checkpoint_pool(dep) as checkpoints:
                fence, thread = await begin(
                    state, actor, conversation, a, question="帮我看销售情况"
                )
                model = AnalysisModel(
                    settings.model_copy(update={"model_base_url": provider.url}), state
                )
                saver = FencedPostgresSaver(checkpoints)
                compiled = build_graph(model, DataToolsClient(settings, state), saver)
                cfg = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
                try:
                    with checkpoint_scope(actor, fence, thread):
                        paused = await compiled.ainvoke({"question": "帮我看销售情况"}, cfg)
                    assert paused.get("__interrupt__")
                    payload = paused["__interrupt__"][0].value
                    assert payload["kind"] == "clarification"
                    await RunRepository(state).pause(fence, review_payload=payload)
                    review = await ConfirmationService(state, settings.mcp_signing_key).issue(
                        actor, fence.run_id
                    )
                    await RunService(state).submit(
                        actor, conversation, b, uuid4(), "确认", Scope.BACKGROUND
                    )
                    waiting = await RunService(state).get(actor, fence.run_id)
                    assert waiting.state == "WAITING_INPUT" and waiting.model_used == 1
                    key = ResumeKey(idempotency_key=uuid4(), token=review["token"])
                    service = ConfirmationService(state, settings.mcp_signing_key)
                    decision = {"action": "clarify", "answer": "按实付销售额统计全部已支付订单"}
                    receipt = await service.resume(
                        actor, fence.run_id, review["confirmation_id"], decision, key
                    )
                    assert (
                        await service.resume(
                            actor, fence.run_id, review["confirmation_id"], decision, key
                        )
                        == receipt
                    )
                    fresh = await RunRepository(state).claim(fence.run_id, uuid4())
                    assert fresh is not None
                    async with state.connection() as conn:
                        row = await (
                            await conn.execute(
                                "SELECT resume_payload,attempts,model_used FROM app_state.runs "
                                "WHERE id=%s",
                                (fence.run_id,),
                            )
                        ).fetchone()
                    assert row["attempts"] == 1 and row["model_used"] == 1
                    with checkpoint_scope(actor, fresh, thread):
                        done = await compiled.ainvoke(Command(resume=row["resume_payload"]), cfg)
                    assert done["status"] == "complete" and done["report"]["claims"]
                    assert (await RunService(state).get(actor, fence.run_id)).model_used == 4
                finally:
                    await model.aclose()

        run(check())
        assert len(provider.requests) == 4
        import json

        refined_plan = json.loads(provider.requests[1]["messages"][-1]["content"])["input"]
        refined_sql = json.loads(provider.requests[2]["messages"][-1]["content"])["input"]
        answer = "按实付销售额统计全部已支付订单"
        assert answer in refined_plan["question"]
        assert answer in refined_sql["context"]["question"]
        assert answer not in provider.requests[1]["messages"][0]["content"]


def test_real_cost_interrupt_uses_bound_query_and_resume_does_not_call_model_twice(agent_case):
    dep, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    sql = (
        "SELECT COUNT(*) AS order_count FROM analysis.orders a CROSS JOIN "
        "analysis.orders b CROSS JOIN analysis.orders c"
    )

    def description(body):
        import json

        fact = next(
            cell
            for cell in json.loads(body["messages"][-1]["content"])["input"]["cells"]
            if cell["column"] == "order_count"
        )
        return {
            "claims": [
                {
                    "evidence_id": fact["evidence_id"],
                    "column": "order_count",
                    "value": fact["value"],
                    "text_template": "本次只读查询结果为 {value}",
                }
            ]
        }

    with ControlledProvider([plan(), query(sql), description]) as provider:

        async def check():
            async with state_pool(dep) as state, checkpoint_pool(dep) as checkpoints:
                fence, thread = await begin(state, actor, conversation, a)
                model = AnalysisModel(
                    settings.model_copy(update={"model_base_url": provider.url}), state
                )
                cfg = {"configurable": {"thread_id": thread}}
                service = ConfirmationService(state, settings.mcp_signing_key)
                try:
                    graph = build_graph(
                        model, DataToolsClient(settings, state), FencedPostgresSaver(checkpoints)
                    )
                    with checkpoint_scope(actor, fence, thread):
                        paused = await graph.ainvoke({"question": "count"}, cfg)
                    payload = paused["__interrupt__"][0].value
                    assert payload["kind"] == "query_approval"
                    await RunRepository(state).pause(
                        fence, query_id=UUID(payload["query_id"]), review_payload=payload
                    )
                    review = await service.issue(actor, fence.run_id)
                    assert review["explain"]["total_cost"] > 10000
                    key = ResumeKey(idempotency_key=uuid4(), token=review["token"])
                    receipt = await service.resume(
                        actor, fence.run_id, review["confirmation_id"], {"action": "approve"}, key
                    )
                    assert receipt == await service.resume(
                        actor, fence.run_id, review["confirmation_id"], {"action": "approve"}, key
                    )
                    fresh = await RunRepository(state).claim(fence.run_id, uuid4())
                    async with state.connection() as conn:
                        row = await (
                            await conn.execute(
                                "SELECT resume_payload FROM app_state.runs WHERE id=%s",
                                (fence.run_id,),
                            )
                        ).fetchone()
                    with checkpoint_scope(actor, fresh, thread):
                        done = await graph.ainvoke(Command(resume=row["resume_payload"]), cfg)
                    assert (
                        done["status"] == "complete"
                        and done["report"]["claims"][0]["value"] == 96**3
                    )
                    snapshot = await RunService(state).get(actor, fence.run_id)
                    assert (
                        snapshot.model_used == 3
                        and snapshot.query_used == 1
                        and snapshot.attempts == 1
                    )
                finally:
                    await model.aclose()

        run(check())
        assert len(provider.requests) == 3


def test_clarification_http_requires_cookie_csrf_bound_token_and_idempotency(agent_case):
    from fastapi.testclient import TestClient

    from insightpilot.api import create_app
    from insightpilot.runtime import new_event_loop
    from tests.support import PASSWORD

    dep, actors, (conversation, a, b), settings = agent_case
    actor = actors["A_admin"]
    with ControlledProvider([plan(True)]) as provider:

        async def pause():
            async with state_pool(dep) as state, checkpoint_pool(dep) as checkpoints:
                fence, thread = await begin(state, actor, conversation, a)
                model = AnalysisModel(
                    settings.model_copy(update={"model_base_url": provider.url}), state
                )
                try:
                    graph = build_graph(
                        model, DataToolsClient(settings, state), FencedPostgresSaver(checkpoints)
                    )
                    with checkpoint_scope(actor, fence, thread):
                        paused = await graph.ainvoke(
                            {"question": "ambiguous"}, {"configurable": {"thread_id": thread}}
                        )
                    await RunRepository(state).pause(
                        fence, review_payload=paused["__interrupt__"][0].value
                    )
                    return str(fence.run_id)
                finally:
                    await model.aclose()

        run_id = run(pause())
        with TestClient(
            create_app(settings),
            backend_options={"loop_factory": new_event_loop},
            headers={"origin": "http://localhost:8090"},
        ) as http:
            response = http.post(
                "/api/v1/auth/login",
                json={
                    "tenant_id": str(actor.tenant_id),
                    "email": "admin@a.synthetic.invalid",
                    "password": PASSWORD,
                },
            )
            assert response.status_code == 200
            review = http.get(f"/api/v1/runs/{run_id}/confirmation")
            assert review.status_code == 200 and review.json()["kind"] == "clarification"
            review = review.json()
            body = {
                "confirmation_id": review["confirmation_id"],
                "idempotency_key": str(uuid4()),
                "token": review["token"],
                "decision": {"action": "clarify", "answer": "统计全部已支付订单实付销售额"},
            }
            route = f"/api/v1/runs/{run_id}/resume"
            assert http.post(route, json=body).status_code == 403
            headers = {"x-csrf-token": http.cookies.get("ip_csrf")}
            bad = {**body, "token": "invalid"}
            assert http.post(route, json=bad, headers=headers).status_code == 409
            accepted = http.post(route, json=body, headers=headers)
            assert accepted.status_code == 202
            assert http.post(route, json=body, headers=headers).json() == accepted.json()
            changed = {**body, "decision": {"action": "clarify", "answer": "改成未支付订单"}}
            assert http.post(route, json=changed, headers=headers).status_code == 409
            snapshot = http.get(f"/api/v1/runs/{run_id}").json()
            assert snapshot["state"] == "QUEUED" and snapshot["model_used"] == 1
