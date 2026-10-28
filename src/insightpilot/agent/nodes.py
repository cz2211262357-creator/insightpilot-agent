"Small replayable graph nodes; scopes come from the current Worker, not checkpoints."

from uuid import uuid4

from langgraph.types import interrupt

from insightpilot.admission import enforce_execution_permit
from insightpilot.agent.state import PlanningInput, QueryStep, SqlContext
from insightpilot.catalog.models import Catalog
from insightpilot.checkpoints import current_checkpoint_scope
from insightpilot.errors import DomainError
from insightpilot.query.repository import authorized_scope
from insightpilot.reports.builder import (
    CHART_FALLBACK_WARNING,
    TEXT_REFORMATTED_WARNING,
    ReportBuilder,
)
from insightpilot.reports.models import FactCell, ReportFacts
from insightpilot.runs.repository import RunRepository, emit, execution_row

REPAIRABLE = {
    "sql_column_forbidden",
    "sql_execution_invalid",
    "sql_parse_invalid",
    "sql_timeout",
    "sql_syntax_invalid",
    "sql_schema_forbidden",
    "sql_unsafe_grain",
    "sql_expression_unsupported",
    "sql_table_forbidden",
}
TRANSIENT = {"provider_timeout", "provider_unavailable", "mcp_unavailable", "database_unavailable"}


def effective_question(state):
    original = state["question"]
    clarifications = state.get("clarifications", [])
    if not clarifications:
        return original
    # This remains lower-priority user data, not a system instruction.
    return "原始问题：" + original + "\n用户已澄清：" + "\n".join(clarifications)


class AgentNodes:
    def __init__(self, model, tools):
        self.model, self.tools, self.pool = model, tools, model.pool

    def guarded(self, node):
        async def execute(state):
            try:
                await enforce_execution_permit()
                result = await node(state)
                scope = current_checkpoint_scope()
                async with self.pool.connection() as conn, conn.transaction():
                    row = await execution_row(conn, scope.fence)
                    public = {
                        "stage": node.__name__,
                        "status": result.get("status", state.get("status")),
                    }
                    if "plan" in result:
                        public["plan"] = [
                            {k: step[k] for k in ("id", "goal", "metric_ids")}
                            for step in result["plan"]["steps"]
                        ]
                    if "query_id" in result:
                        public["query_id"] = result["query_id"]
                    await emit(conn, row, "analysis.phase", public)
                return result
            except DomainError as error:
                if error.status >= 500 or error.status in (401, 403, 404, 409):
                    raise RuntimeError(error.code) from None
                return {"status": "failed", "error_code": error.code, "report": None}
            except RuntimeError as error:
                if len(error.args) != 1 or error.args[0] not in {
                    *TRANSIENT,
                    "model_output_invalid",
                    "provider_authorization_denied",
                    "provider_request_rejected",
                }:
                    raise
                if error.args[0] in TRANSIENT:
                    raise
                return {"status": "failed", "error_code": error.args[0], "report": None}

        return execute

    async def tool(self, name, args):
        result = await self.tools.call(current_checkpoint_scope().fence, name, args)
        if not result.ok:
            raise DomainError(result.error["code"], result.error["status"])
        return result.data

    async def bootstrap(self, state):
        scope = current_checkpoint_scope()
        async with self.pool.connection() as conn, conn.transaction():
            row, catalog = await authorized_scope(conn, scope.actor, scope.fence)
            history = await (
                await conn.execute(
                    """
                SELECT m.role,m.content FROM app_state.messages m
                JOIN app_state.runs r ON r.tenant_id=m.tenant_id AND r.id=m.run_id
                WHERE m.tenant_id=%s AND m.topic_id=%s AND m.status='completed'
                AND r.state='SUCCEEDED' AND r.user_id=%s AND r.id<>%s AND r.dataset_version=%s
                ORDER BY m.created_at DESC,m.id DESC LIMIT 20
            """,
                    (
                        scope.actor.tenant_id,
                        scope.fence.topic_id,
                        scope.actor.user_id,
                        scope.fence.run_id,
                        row["dataset_version"],
                    ),
                )
            ).fetchall()
        # MCP obtains a current catalog under the same identity, never a model URL.
        catalog = await self.tool("list_datasets", {})
        clipped, remaining = [], 16000
        for turn in history:
            if remaining <= 0:
                break
            content = turn["content"][-remaining:]
            clipped.append({"role": turn["role"], "content": content})
            remaining -= len(content)
        return {
            "question": row["question"],
            "catalog": catalog,
            "history": list(reversed(clipped)),
            "clarifications": [],
            "step_index": 0,
            "evidence_ids": [],
            "charts": {},
            "status": "planning",
            "error_code": None,
        }

    async def plan(self, state):
        result = await self.model.plan(
            PlanningInput(
                question=effective_question(state),
                catalog=Catalog.model_validate(state["catalog"]),
                history=state["history"],
                clarifications=state["clarifications"],
            )
        )
        if result.needs_clarification:
            if not result.clarification_question or len(result.clarification_question) > 500:
                raise DomainError("plan_clarification_invalid", 422)
        elif (
            not result.steps
            or not result.metric_ids
            or [step.id for step in result.steps] != [f"s{i + 1}" for i in range(len(result.steps))]
        ):
            raise DomainError("plan_steps_invalid", 422)
        return {
            "plan": result.model_dump(mode="json"),
            "status": "planned",
            "review_nonce": str(uuid4()),
        }

    async def clarification(self, state):
        decision = interrupt(
            {
                "kind": "clarification",
                "question": state["plan"]["clarification_question"],
                "nonce": state["review_nonce"],
            }
        )
        scope = current_checkpoint_scope()
        async with self.pool.connection() as conn, conn.transaction():
            row, _ = await authorized_scope(conn, scope.actor, scope.fence)
            if (
                not isinstance(decision, dict)
                or row["resume_payload"] != decision
                or decision.get("kind") != "clarification"
                or decision.get("nonce") != state["review_nonce"]
                or not row["resume_review_id"]
                or str(row["resume_review_id"]) != decision.get("review_id")
            ):
                raise DomainError("confirmation_stale", 409)
        return {
            "clarifications": [*state["clarifications"], decision["answer"]],
            "status": "planning",
        }

    async def generate(self, state):
        step = QueryStep.model_validate(state["plan"]["steps"][state["step_index"]])
        definitions = [
            await self.tool("get_metric_definition", {"metric_id": m}) for m in step.metric_ids
        ]
        result = await self.model.sql(
            step,
            SqlContext(
                question=effective_question(state),
                catalog=Catalog.model_validate(state["catalog"]),
                definitions=definitions,
                clarifications=state["clarifications"],
                previous_sql=state.get("generated", {}).get("sql")
                if state.get("error_code")
                else None,
                error_code=state.get("error_code"),
            ),
        )
        return {
            "generated": result.model_dump(mode="json"),
            "error_code": None,
            "status": "validating",
        }

    async def validate(self, state):
        try:
            ticket = await self.tool("validate_query", state["generated"])
        except DomainError as error:
            if error.code in REPAIRABLE and error.status == 422:
                return {"error_code": error.code, "status": "repairing"}
            raise
        return {
            "query_id": ticket["query_id"],
            "approval_id": None,
            "requires_approval": ticket["requires_approval"],
            "status": "validated",
        }

    async def repair(self, state):
        scope = current_checkpoint_scope()
        async with self.pool.connection() as conn, conn.transaction():
            row, _ = await authorized_scope(conn, scope.actor, scope.fence)
            if row["repair_used"] >= 2:
                return {"status": "failed", "error_code": "sql_repair_exhausted", "report": None}
        await RunRepository(self.pool).reserve(scope.fence, "repair")
        return {"status": "repairing"}

    async def cost_review(self, state):
        decision = interrupt({"kind": "query_approval", "query_id": state["query_id"]})
        scope = current_checkpoint_scope()
        async with self.pool.connection() as conn, conn.transaction():
            row, _ = await authorized_scope(conn, scope.actor, scope.fence)
            if (
                not isinstance(decision, dict)
                or row["resume_payload"] != decision
                or decision.get("kind") != "query_approval"
                or decision.get("query_id") != state["query_id"]
                or not row["resume_confirmation_id"]
                or str(row["resume_confirmation_id"]) != decision.get("approval_id")
            ):
                raise DomainError("query_approval_stale", 409)
        return {"approval_id": decision["approval_id"], "status": "validated"}

    async def execute(self, state):
        try:
            evidence = await self.tool(
                "execute_query",
                {"query_id": state["query_id"], "approval_id": state.get("approval_id")},
            )
        except DomainError as error:
            if error.code in REPAIRABLE and error.status == 422:
                return {"error_code": error.code, "status": "repairing"}
            raise
        identity = evidence["evidence_id"]
        step = state["plan"]["steps"][state["step_index"]]
        return {
            "evidence_ids": [*state["evidence_ids"], identity],
            "charts": {**state["charts"], identity: step["chart"]},
            "step_index": state["step_index"] + 1,
            "status": "executed",
        }

    async def evidence(self, state):
        from insightpilot.reports.repository import load_for_run

        scope = current_checkpoint_scope()
        async with self.pool.connection() as conn, conn.transaction():
            return await load_for_run(conn, scope.actor, scope.fence, state["evidence_ids"])

    async def describe(self, state):
        evidence, flags, catalog = await self.evidence(state)
        cells = []
        warnings = []
        for item in evidence:
            if item.truncated or flags[str(item.evidence_id)]["limited"]:
                warnings.append("此结果包含行数限制，不代表全部数据。")
            for index, row in enumerate(item.rows):
                for column in item.columns:
                    if len(cells) >= 80:
                        break
                    value = row.get(column)
                    if isinstance(value, str):
                        value = value[:512]
                    cells.append(
                        FactCell(
                            evidence_id=item.evidence_id,
                            row_index=index,
                            column=column,
                            value=value,
                            label=column,
                            unit="",
                        )
                    )
                if len(cells) >= 80:
                    break
        narrative = await self.model.describe(
            ReportFacts(question=effective_question(state), cells=cells, warnings=warnings)
        )
        # Reload again after the model call, before constructing publishable output.
        evidence, flags, catalog = await self.evidence(state)
        claims = narrative.claims
        chart_specs = state["charts"]
        fallbacks = []
        for _ in range(3):
            try:
                report = ReportBuilder(chart_specs=chart_specs, query_flags=flags).build(
                    evidence, claims
                )
                break
            except DomainError as error:
                if (
                    error.code == "report_text_invalid"
                    and TEXT_REFORMATTED_WARNING not in fallbacks
                ):
                    # The model's free text is discarded; cited values still
                    # undergo the original evidence validation on the next pass.
                    claims = [
                        claim.model_copy(update={"text_template": "查询结果为 {value}"})
                        for claim in claims
                    ]
                    fallbacks.append(TEXT_REFORMATTED_WARNING)
                elif (
                    error.code in {"report_chart_column_invalid", "report_chart_value_invalid"}
                    and CHART_FALLBACK_WARNING not in fallbacks
                ):
                    # Tables are generated solely from the approved evidence.
                    chart_specs = {identity: {"kind": "table"} for identity in chart_specs}
                    fallbacks.append(CHART_FALLBACK_WARNING)
                else:
                    raise
        else:
            raise DomainError("report_fallback_exhausted", 422)
        if fallbacks:
            report = report.model_copy(update={"warnings": [*report.warnings, *fallbacks]})
        return {
            "status": "complete",
            "error_code": None,
            "charts": chart_specs,
            "report": report.model_dump(mode="json"),
        }
