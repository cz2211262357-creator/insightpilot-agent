"Actual readonly SQL, persistent execution reservation, reauthorized evidence."

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import psycopg
import sqlglot
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from sqlglot import exp

from insightpilot.errors import DomainError
from insightpilot.query.confirmations import ConfirmationService, require_approval
from insightpilot.query.evidence import (
    MAX_RESULT_BYTES,
    MAX_ROWS,
    TYPE_NAMES,
    build_evidence,
    encoded,
    scalar,
)
from insightpilot.query.models import QueryTicket
from insightpilot.query.parameters import compile_bound
from insightpilot.query.policy import SqlPolicy
from insightpilot.query.repository import (
    authorized_scope,
    existing_evidence,
    save_evidence,
    ticket_for_run,
)
from insightpilot.runs.repository import terminal


def database_error(error):
    if isinstance(error, (TimeoutError, psycopg.errors.QueryCanceled)):
        return DomainError("sql_timeout", 422)
    if isinstance(
        error,
        (
            psycopg.OperationalError,
            psycopg.InterfaceError,
            PoolTimeout,
            psycopg.errors.IdleInTransactionSessionTimeout,
            psycopg.errors.LockNotAvailable,
        ),
    ):
        return DomainError("database_unavailable", 503)
    if isinstance(error, psycopg.errors.InsufficientPrivilege):
        return DomainError("sql_permission_denied", 403)
    return DomainError("sql_execution_invalid", 422)


class QueryGateway:
    def __init__(self, state_pool, query_pool, signing_key):
        self.state_pool = state_pool
        self.query_pool = query_pool
        self.signing_key = signing_key
        self.policy = SqlPolicy()

    @asynccontextmanager
    async def _reader(self, query, actor, seconds_remaining=3):
        try:
            async with asyncio.timeout(min(3, seconds_remaining)):
                async with self.query_pool.connection() as conn, conn.transaction():
                    await conn.execute("SET TRANSACTION READ ONLY")
                    await conn.execute("SET LOCAL search_path=analysis,pg_catalog")
                    await conn.execute(
                        "SELECT set_config('insightpilot.tenant_id',%s,true),set_config('i"
                        "nsightpilot.dataset_version',%s,true),set_config('statement_timeo"
                        "ut',%s,true)",
                        (
                            str(actor.tenant_id),
                            str(query.dataset_version),
                            str(max(1, min(3000, int(seconds_remaining * 1000)))),
                        ),
                    )
                    yield conn
        except (TimeoutError, psycopg.Error, PoolTimeout) as error:
            raise database_error(error) from None

    async def _explain(self, query, actor):
        sql, params = compile_bound(query)
        async with self._reader(query, actor) as conn:
            row = await (
                await conn.execute("EXPLAIN (FORMAT JSON, COSTS TRUE) " + sql, params)
            ).fetchone()
        root = row["QUERY PLAN"][0]["Plan"]
        cost = float(root["Total Cost"])
        rows = int(root["Plan Rows"])
        reasons = []
        if cost > 10000:
            reasons.append("total_cost")
        if rows > 100000:
            reasons.append("estimated_rows")
        return {"total_cost": cost, "estimated_rows": rows, "approval_reasons": reasons}

    async def validate(self, actor, fence, sql, params):
        async with self.state_pool.connection() as conn, conn.transaction():
            _, catalog = await authorized_scope(conn, actor, fence)
        query = self.policy.validate(sql, params, catalog)
        explain = await self._explain(query, actor)
        identity = uuid4()
        async with self.state_pool.connection() as conn, conn.transaction():
            _, current = await authorized_scope(conn, actor, fence)
            if (
                current.version != query.catalog_version
                or current.dataset_version != query.dataset_version
            ):
                raise DomainError("query_stale", 409)
            await conn.execute(
                """
                INSERT INTO app_state.query_tickets(id,tenant_id,run_id,task_revision,
                  auth_version,dataset_version,catalog_version,policy_version,sql,params,
                  sql_hash,requires_approval,explain)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    identity,
                    actor.tenant_id,
                    fence.run_id,
                    fence.task_revision,
                    actor.auth_version,
                    query.dataset_version,
                    query.catalog_version,
                    query.policy_version,
                    query.sql,
                    Jsonb(query.params),
                    query.sql_hash,
                    bool(explain["approval_reasons"]),
                    Jsonb(explain),
                ),
            )
        return QueryTicket(
            query_id=identity,
            sql_hash=query.sql_hash,
            requires_approval=bool(explain["approval_reasons"]),
            explain=explain,
        )

    async def _fetch(self, query, actor, seconds_remaining):
        sql, params = compile_bound(query)
        async with self._reader(query, actor, seconds_remaining) as conn:
            async with conn.cursor(name="analysis_" + uuid4().hex) as cursor:
                await cursor.execute(sql, params)
                columns = [field.name for field in cursor.description]
                if len(set(columns)) != len(columns):
                    raise DomainError("sql_duplicate_columns", 422)
                types = {
                    field.name: TYPE_NAMES.get(field.type_code, "unsupported")
                    for field in cursor.description
                }
                rows = []
                size = 0
                reserve = len(encoded({"columns": columns, "types": types})) + 2048
                while True:
                    batch = await cursor.fetchmany(32)
                    if not batch:
                        break
                    for raw in batch:
                        row = {name: scalar(value) for name, value in raw.items()}
                        weight = len(encoded(row)) + 1
                        if len(rows) >= MAX_ROWS or size + weight > MAX_RESULT_BYTES - reserve:
                            return {
                                "columns": columns,
                                "types": types,
                                "rows": rows,
                                "truncated": True,
                            }
                        rows.append(row)
                        size += weight
                return {"columns": columns, "types": types, "rows": rows, "truncated": False}

    async def _mark_failure(self, actor, fence, query_id, error):
        try:
            async with self.state_pool.connection() as conn, conn.transaction():
                await authorized_scope(conn, actor, fence)
                await conn.execute(
                    """
                    UPDATE app_state.query_tickets SET execution_state=%s,error_code=%s,
                      execution_until=NULL WHERE tenant_id=%s AND run_id=%s AND id=%s
                      AND execution_version=%s AND execution_owner=%s
                    """,
                    (
                        "READY" if error.status == 503 else "FAILED",
                        error.code,
                        actor.tenant_id,
                        fence.run_id,
                        query_id,
                        fence.execution_version,
                        fence.owner,
                    ),
                )
        except (DomainError, psycopg.Error, PoolTimeout):
            # Durable leases govern recovery if even the error cannot be recorded.
            pass

    async def execute(self, actor, fence, query_id, approval_id=None):
        error = None
        async with self.state_pool.connection() as conn, conn.transaction():
            run, catalog = await authorized_scope(conn, actor, fence)
            ticket = await ticket_for_run(conn, actor, fence, query_id, True)
            query = self.policy.validate(ticket["sql"], ticket["params"], catalog)
            if (
                query.sql_hash != ticket["sql_hash"]
                or query.policy_version != ticket["policy_version"]
            ):
                raise DomainError("query_stale", 409)
            existing = await existing_evidence(conn, actor, fence, query_id)
            if existing:
                return existing
            if ticket["requires_approval"]:
                await require_approval(conn, actor, fence, run, ticket, approval_id)
            if ticket["execution_state"] == "FAILED":
                raise DomainError(ticket["error_code"] or "sql_execution_invalid", 422)
            now = (await (await conn.execute("SELECT clock_timestamp() AS now")).fetchone())["now"]
            if (
                ticket["execution_state"] == "EXECUTING"
                and ticket["execution_version"] == fence.execution_version
                and ticket["execution_owner"] == fence.owner
                and ticket["execution_until"] > now
            ):
                raise DomainError("query_in_progress", 409)
            if run["query_used"] >= 4:
                await terminal(conn, run, "FAILED", "run_budget_exhausted")
                error = DomainError("run_budget_exhausted", 409)
            else:
                remaining = await (
                    await conn.execute(
                        """
                    UPDATE app_state.runs SET query_used=query_used+1 WHERE id=%s
                    RETURNING EXTRACT(EPOCH FROM(active_deadline-clock_timestamp())) AS seconds
                    """,
                        (fence.run_id,),
                    )
                ).fetchone()
                timeout = min(3, float(remaining["seconds"]))
                await conn.execute(
                    """
                    UPDATE app_state.query_tickets SET execution_state='EXECUTING',
                      execution_version=%s,execution_owner=%s,
                      execution_until=clock_timestamp()+interval '10 seconds',error_code=NULL
                    WHERE tenant_id=%s AND run_id=%s AND id=%s
                    """,
                    (fence.execution_version, fence.owner, actor.tenant_id, fence.run_id, query_id),
                )
        if error:
            raise error
        try:
            result = await self._fetch(query, actor, timeout)
        except DomainError as failure:
            await self._mark_failure(actor, fence, query_id, failure)
            raise
        aliases = {
            alias.alias
            for alias in sqlglot.parse_one(query.sql, read="postgres").find_all(exp.Alias)
        }
        metrics = sorted(aliases & {metric.metric_id for metric in catalog.metrics})
        evidence = build_evidence(query_id, query, result, metrics)
        async with self.state_pool.connection() as conn, conn.transaction():
            await authorized_scope(conn, actor, fence)
            current = await ticket_for_run(conn, actor, fence, query_id, True)
            now = (await (await conn.execute("SELECT clock_timestamp() AS now")).fetchone())["now"]
            if (
                current["execution_version"] != fence.execution_version
                or current["execution_owner"] != fence.owner
                or current["execution_until"] is None
                or current["execution_until"] <= now
            ):
                raise DomainError("stale_query_execution", 409)
            saved = await save_evidence(conn, actor, fence, evidence)
            committed = await (
                await conn.execute(
                    """
                    UPDATE app_state.query_tickets t SET
                      execution_state='DONE',execution_until=NULL
                    WHERE t.id=%s AND t.tenant_id=%s AND t.run_id=%s
                      AND t.execution_version=%s AND t.execution_owner=%s
                      AND t.execution_state='EXECUTING' AND t.execution_until>clock_timestamp()
                      AND EXISTS (
                        SELECT 1 FROM app_state.runs r WHERE r.tenant_id=t.tenant_id
                          AND r.id=t.run_id AND r.topic_id=%s AND r.task_revision=%s
                          AND r.execution_version=%s AND r.owner=%s AND r.auth_version=%s
                          AND r.state='RUNNING' AND r.lease_until>clock_timestamp()
                          AND r.active_deadline>clock_timestamp()
                      ) RETURNING t.id
                    """,
                    (
                        query_id,
                        actor.tenant_id,
                        fence.run_id,
                        fence.execution_version,
                        fence.owner,
                        fence.topic_id,
                        fence.task_revision,
                        fence.execution_version,
                        fence.owner,
                        fence.auth_version,
                    ),
                )
            ).fetchone()
            if committed is None:
                # Roll back the evidence INSERT as well as the ticket transition.
                raise DomainError("stale_query_execution", 409)
        return saved

    async def create_confirmation(self, actor, run_id, query_id):
        return await ConfirmationService(self.state_pool, self.signing_key).create(
            actor, run_id, query_id
        )

    async def approve(self, actor, confirmation_id, token):
        return await ConfirmationService(self.state_pool, self.signing_key).approve(
            actor, confirmation_id, token
        )
