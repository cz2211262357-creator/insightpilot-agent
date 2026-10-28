from uuid import uuid4

from insightpilot.runs.service import RunService
from insightpilot.worker import AnalysisWorker
from tests.controlled_provider import ControlledProvider, narrative, plan, query
from tests.integration.test_agent_graph import agent_case as agent_case
from tests.integration.test_mcp_http import mcp_process as mcp_process
from tests.integration.test_query_gateway import start, systems
from tests.integration.test_run_races import scenario as scenario
from tests.support import run
from tests.system.test_execution_recovery import opened


def test_one_invalid_model_format_is_repaired_with_durable_budget_and_usage(agent_case):
    _, actors, (conversation, topic, _), settings = agent_case
    with ControlledProvider(
        [{"_raw_content": "PRIVATE_FORMAT_FAILURE not JSON"}, plan(), query(), narrative]
    ) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                service = RunService(pools.postgres["state"])
                receipt = await service.submit(
                    actors["A_admin"],
                    conversation,
                    topic,
                    uuid4(),
                    "统计全部已支付订单实付销售额",
                    "BACKGROUND",
                )
                await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                row = await service.get(actors["A_admin"], receipt.run_id)
                assert row.state == "SUCCEEDED" and row.model_used == 4 and row.query_used == 1
                async with pools.postgres["state"].connection() as conn:
                    calls = await (
                        await conn.execute(
                            "SELECT status,error_code,total_tokens FROM app_state.model_calls "
                            "WHERE run_id=%s ORDER BY reservation",
                            (receipt.run_id,),
                        )
                    ).fetchall()
                assert len(calls) == 4 and calls[0]["error_code"] == "model_output_invalid"
                assert [c["status"] for c in calls] == ["FAILED"] + ["SUCCEEDED"] * 3
                assert sum(c["total_tokens"] for c in calls) == 200

        run(check())
        assert len(provider.requests) == 4
        assert "PRIVATE_FORMAT_FAILURE" not in provider.requests[1]["messages"][0]["content"]
        assert "PRIVATE_FORMAT_FAILURE" not in provider.requests[1]["messages"][-1]["content"]


def test_model_format_correction_stops_after_one_retry_and_never_runs_sql(agent_case):
    _, actors, (conversation, topic, _), settings = agent_case
    with ControlledProvider(
        [
            {"_raw_content": "PRIVATE_FORMAT_FAILURE not JSON"},
            {"_raw_content": "PRIVATE_FORMAT_FAILURE again not JSON"},
        ]
    ) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                service = RunService(pools.postgres["state"])
                receipt = await service.submit(
                    actors["A_admin"],
                    conversation,
                    topic,
                    uuid4(),
                    "统计全部实付销售额",
                    "BACKGROUND",
                )
                await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                row = await service.get(actors["A_admin"], receipt.run_id)
                assert row.state == "FAILED" and row.error_code == "model_output_invalid"
                assert row.model_used == 2 and row.query_used == 0 and row.attempts == 1

        run(check())
        assert len(provider.requests) == 2


def test_safe_postgres_date_conversion_executes_like_cast_under_rls(scenario):
    dep, actors, (conversation, topic, _) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, _, gateway):
            fence = await start(state, actor, conversation, topic)
            outputs = []
            for expression in (
                "CAST(paid_at AT TIME ZONE 'Asia/Shanghai' AS DATE)",
                "DATE(paid_at AT TIME ZONE 'Asia/Shanghai')",
            ):
                ticket = await gateway.validate(
                    actor,
                    fence,
                    "SELECT " + expression + " AS day, SUM(paid_amount) AS paid_sales "
                    "FROM analysis.orders WHERE status='paid' GROUP BY day ORDER BY day",
                    {},
                )
                outputs.append(await gateway.execute(actor, fence, ticket.query_id))
            assert outputs[0].rows and outputs[0].rows == outputs[1].rows
            assert outputs[0].types["day"] == "date" and not outputs[1].truncated

    run(check())


def test_report_reformats_unsafe_model_prose_without_accepting_invented_numbers(agent_case):
    _, actors, (conversation, topic, _), settings = agent_case

    def unsafe_text(body):
        result = narrative(body)
        result["claims"][0]["text_template"] = "八月销售额是 999 元，真实结果 {value}"
        return result

    with ControlledProvider([plan(), query(), unsafe_text]) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                service = RunService(pools.postgres["state"])
                receipt = await service.submit(
                    actors["A_admin"],
                    conversation,
                    topic,
                    uuid4(),
                    "统计全部已支付订单实付销售额",
                    "BACKGROUND",
                )
                await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                result = await service.get(actors["A_admin"], receipt.run_id)
                assert result.state == "SUCCEEDED" and result.report
                assert "999" not in result.report["summary"]
                assert "八月" not in result.report["summary"]
                assert (
                    result.report["claims"][0]["value"]
                    == result.report["evidence"][0]["rows"][0]["paid_sales"]
                )
                assert any("说明" in warning for warning in result.report["warnings"])
                from insightpilot.reports.history import HistoricalReports

                history = await HistoricalReports(pools.postgres["state"]).report(
                    actors["A_admin"], receipt.run_id
                )
                assert any("说明" in warning for warning in history.warnings)

        run(check())


def test_text_fallback_does_not_override_wrong_numeric_claim(agent_case):
    _, actors, (conversation, topic, _), settings = agent_case

    def unsafe_value(body):
        result = narrative(body)
        result["claims"][0]["text_template"] = "八月结果 {value}"
        result["claims"][0]["value"] = "999999999"
        return result

    with ControlledProvider([plan(), query(), unsafe_value]) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                service = RunService(pools.postgres["state"])
                receipt = await service.submit(
                    actors["A_admin"],
                    conversation,
                    topic,
                    uuid4(),
                    "统计全部已支付订单实付销售额",
                    "BACKGROUND",
                )
                await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                result = await service.get(actors["A_admin"], receipt.run_id)
                assert result.state == "FAILED" and result.error_code == "report_value_mismatch"
                assert result.report is None

        run(check())


def test_one_to_many_order_aggregates_are_rejected_but_line_amounts_work(scenario):
    import pytest

    from insightpilot.errors import DomainError

    dep, actors, (conversation, topic, _) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, _, gateway):
            fence = await start(state, actor, conversation, topic)
            bad = [
                "SELECT p.category, SUM(o.paid_amount) AS paid_sales FROM analysis.orders o "
                "JOIN analysis.order_items i ON o.order_id=i.order_id JOIN analysis.products p "
                "ON i.product_id=p.product_id WHERE o.status='paid' GROUP BY p.category",
                "SELECT COUNT(o.order_id) AS order_count FROM analysis.orders o "
                "JOIN analysis.order_items i ON o.order_id=i.order_id WHERE o.status='paid'",
                "SELECT COUNT(*) AS order_count FROM analysis.orders o "
                "JOIN analysis.aftersales a ON o.order_id=a.order_id WHERE o.status='paid'",
            ]
            for sql in bad:
                with pytest.raises(DomainError) as rejected:
                    await gateway.validate(actor, fence, sql, {})
                assert rejected.value.code == "sql_unsafe_grain"
            good = (
                "SELECT p.category, SUM(i.line_paid_amount) AS paid_sales "
                "FROM analysis.orders o JOIN analysis.order_items i ON o.order_id=i.order_id "
                "JOIN analysis.products p ON p.product_id=i.product_id "
                "WHERE o.status='paid' GROUP BY p.category ORDER BY p.category"
            )
            ticket = await gateway.validate(actor, fence, good, {})
            result = await gateway.execute(actor, fence, ticket.query_id)
            assert len(result.rows) == 3 and result.rows[0]["paid_sales"] is not None
            distinct = (
                "SELECT COUNT(DISTINCT o.order_id) AS order_count "
                "FROM analysis.orders o JOIN analysis.order_items i ON o.order_id=i.order_id "
                "WHERE o.status='paid'"
            )
            ticket = await gateway.validate(actor, fence, distinct, {})
            assert (await gateway.execute(actor, fence, ticket.query_id)).rows[0]["order_count"] > 0

    run(check())


def test_invalid_chart_fields_fall_back_to_evidence_table(agent_case):
    from insightpilot.reports.history import HistoricalReports

    _, actors, (conversation, topic, _), settings = agent_case
    selected = plan()
    selected["steps"][0]["chart"] = {
        "kind": "bar",
        "x_column": "not_a_real_field",
        "y_column": "paid_sales",
    }
    with ControlledProvider([selected, query(), narrative]) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                service = RunService(pools.postgres["state"])
                receipt = await service.submit(
                    actors["A_admin"],
                    conversation,
                    topic,
                    uuid4(),
                    "统计全部已支付订单实付销售额",
                    "BACKGROUND",
                )
                await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                result = await service.get(actors["A_admin"], receipt.run_id)
                assert result.state == "SUCCEEDED" and result.report
                assert result.report["charts"][0]["data"][0]["type"] == "table"
                assert any("图表" in warning for warning in result.report["warnings"])
                historic = await HistoricalReports(pools.postgres["state"]).report(
                    actors["A_admin"], receipt.run_id
                )
                assert historic.charts[0]["data"][0]["type"] == "table"
                assert any("图表" in warning for warning in historic.warnings)

        run(check())


def test_known_pure_window_expression_can_be_repaired_but_unknown_function_cannot(scenario):
    import pytest

    from insightpilot.errors import DomainError

    dep, actors, (conversation, topic, _) = scenario
    actor = actors["A_admin"]

    async def check():
        async with systems(dep) as (state, _, gateway):
            fence = await start(state, actor, conversation, topic)
            unsupported = (
                "SELECT LAG(paid_amount) OVER (ORDER BY paid_at) AS previous FROM analysis.orders"
            )
            with pytest.raises(DomainError) as rejected:
                await gateway.validate(actor, fence, unsupported, {})
            assert rejected.value.code == "sql_expression_unsupported"
            unsafe = (
                "SELECT LAG(paid_amount) OVER (ORDER BY paid_at), pg_sleep(1) FROM analysis.orders"
            )
            with pytest.raises(DomainError) as rejected:
                await gateway.validate(actor, fence, unsafe, {})
            assert rejected.value.code == "sql_policy_forbidden"
            async with state.connection() as conn:
                count = await (
                    await conn.execute(
                        "SELECT count(*) AS n FROM app_state.query_tickets WHERE run_id=%s",
                        (fence.run_id,),
                    )
                ).fetchone()
            assert count["n"] == 0

    run(check())


def test_known_pure_window_expression_repair_executes_only_the_safe_replacement(agent_case):
    _, actors, (conversation, topic, _), settings = agent_case
    unsupported = "SELECT LAG(paid_amount) OVER (ORDER BY paid_at) FROM analysis.orders"
    with ControlledProvider([plan(), query(unsupported), query(), narrative]) as provider:

        async def check():
            configured = settings.model_copy(update={"model_base_url": provider.url})
            async with opened(configured) as pools:
                service = RunService(pools.postgres["state"])
                receipt = await service.submit(
                    actors["A_admin"],
                    conversation,
                    topic,
                    uuid4(),
                    "统计全部已支付订单实付销售额",
                    "BACKGROUND",
                )
                await AnalysisWorker(configured, pools).run_once("BACKGROUND")
                result = await service.get(actors["A_admin"], receipt.run_id)
                assert result.state == "SUCCEEDED" and result.repair_used == 1
                assert result.query_used == 1 and result.model_used == 4

        run(check())
