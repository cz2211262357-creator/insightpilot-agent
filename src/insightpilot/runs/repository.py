"DB-authoritative leases and persistent budgets; Redis is not a commit guard."

from datetime import timedelta
from uuid import uuid4

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from insightpilot.errors import DomainError
from insightpilot.runs.budget import LIMITS, QUEUE_SECONDS, RemainingBudget
from insightpilot.types import RunFence

ACTIVE = ("QUEUED", "RUNNING", "WAITING_INPUT")
TRANSIENT = {
    "admission_lost",
    "admission_unavailable",
    "provider_unavailable",
    "provider_timeout",
    "database_unavailable",
    "mcp_unavailable",
    "worker_lost",
}


async def require_fence(conn, fence):
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("execution_guard_requires_transaction")
    allowed = await (
        await conn.execute(
            "SELECT app_state.lock_execution(%s,%s,%s,%s,%s,%s,%s) AS allowed",
            (
                fence.tenant_id,
                fence.run_id,
                fence.topic_id,
                fence.task_revision,
                fence.execution_version,
                fence.owner,
                fence.auth_version,
            ),
        )
    ).fetchone()
    if not allowed["allowed"]:
        raise DomainError("stale_execution", 409)
    # Checkpoint connections intentionally have no state-table SELECT. The SQL
    # function still holds all locks until their outer transaction commits.
    return None


async def execution_row(conn, fence):
    await require_fence(conn, fence)
    return await (
        await conn.execute(
            "SELECT * FROM app_state.runs WHERE tenant_id=%s AND id=%s",
            (fence.tenant_id, fence.run_id),
        )
    ).fetchone()


async def emit(conn, row, kind, payload=None):
    await conn.execute(
        """
        INSERT INTO app_state.events(tenant_id,run_id,topic_id,epoch,task_revision,
          execution_version,scope,kind,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """,
        (
            row["tenant_id"],
            row["id"],
            row["topic_id"],
            row["epoch"],
            row["task_revision"],
            row["execution_version"],
            row["scope"],
            kind,
            Jsonb(payload or {"state": row["state"]}),
        ),
    )


async def terminal(conn, row, state, error_code=None, bump_revision=False):
    current = await (
        await conn.execute(
            "UPDATE app_state.runs SET "
            "state=%s,error_code=%s,task_revision=task_revision+%s,owner=NULL,"
            "lease_until=NULL,waiting_until=NULL,ended_at=clock_timestamp() "
            "WHERE tenant_id=%s AND id=%s RETURNING *",
            (state, error_code, int(bump_revision), row["tenant_id"], row["id"]),
        )
    ).fetchone()
    await emit(conn, current, "run." + state.lower())
    return current


async def lock_system_run(conn, run_id):
    pointer = await (
        await conn.execute(
            "SELECT tenant_id,user_id,conversation_id FROM app_state.runs WHERE id=%s", (run_id,)
        )
    ).fetchone()
    if pointer is None:
        return None, None, None
    user = await (
        await conn.execute(
            "SELECT * FROM app_state.users WHERE tenant_id=%s AND id=%s FOR SHARE",
            (pointer["tenant_id"], pointer["user_id"]),
        )
    ).fetchone()
    conversation = await (
        await conn.execute(
            "SELECT * FROM app_state.conversations WHERE tenant_id=%s AND id=%s FOR SHARE",
            (pointer["tenant_id"], pointer["conversation_id"]),
        )
    ).fetchone()
    row = await (
        await conn.execute("SELECT * FROM app_state.runs WHERE id=%s FOR UPDATE", (run_id,))
    ).fetchone()
    return user, conversation, row


class RunRepository:
    def __init__(self, pool):
        self.pool = pool

    async def claim(self, run_id, owner):
        async with self.pool.connection() as conn, conn.transaction():
            user, conversation, row = await lock_system_run(conn, run_id)
            if row is None or row["state"] not in ACTIVE:
                return None
            now = (await (await conn.execute("SELECT clock_timestamp() AS now")).fetchone())["now"]
            if not user["enabled"] or user["auth_version"] != row["auth_version"]:
                await terminal(conn, row, "FAILED", "authorization_revoked")
                return None
            if row["scope"] == "INTERACTIVE" and (
                row["epoch"] != conversation["epoch"]
                or row["topic_id"] != conversation["active_topic_id"]
            ):
                await terminal(conn, row, "SUPERSEDED", "foreground_replaced", True)
                return None
            if row["state"] == "WAITING_INPUT":
                if row["waiting_until"] is not None and row["waiting_until"] <= now:
                    await terminal(conn, row, "TIMED_OUT", "confirmation_expired")
                return None
            if row["active_deadline"] is not None and row["active_deadline"] <= now:
                await terminal(conn, row, "TIMED_OUT", "execution_deadline")
                return None
            if row["state"] == "QUEUED" and row["queue_until"] <= now:
                await terminal(conn, row, "TIMED_OUT", "queue_deadline")
                return None
            if row["state"] == "QUEUED" and row["available_at"] > now:
                return None
            if (
                row["state"] == "RUNNING"
                and row["lease_until"] is not None
                and row["lease_until"] > now
            ):
                return None
            if row["state"] == "RUNNING" and row["lease_until"] is not None:
                if row["attempts"] >= 3:
                    await terminal(conn, row, "FAILED", "retry_exhausted")
                    return None
                backoff = 5 if row["attempts"] == 1 else 30
                if now < row["lease_until"] + timedelta(seconds=backoff):
                    return None
            attempts = row["attempts"] + (0 if row["resume_requested"] else 1)
            if attempts > 3:
                await terminal(conn, row, "FAILED", "retry_exhausted")
                return None
            current = await (
                await conn.execute(
                    "UPDATE app_state.runs SET "
                    "state='RUNNING',error_code=NULL,owner=%s,execution_version=execut"
                    "ion_version+1,attempts=%s,total_claims=total_claims+1,resume_requ"
                    "ested=false,active_deadline=COALESCE(active_deadline,clock_timest"
                    "amp()+remaining_seconds*interval '1 "
                    "second'),lease_until=LEAST(COALESCE(active_deadline,clock_timesta"
                    "mp()+remaining_seconds*interval '1 "
                    "second'),clock_timestamp()+interval '60 seconds') WHERE id=%s "
                    "RETURNING *",
                    (owner, attempts, run_id),
                )
            ).fetchone()
            await emit(conn, current, "run.running")
            return RunFence(
                current["tenant_id"],
                run_id,
                current["topic_id"],
                current["task_revision"],
                current["execution_version"],
                owner,
                current["auth_version"],
            )

    async def renew(self, fence):
        try:
            async with self.pool.connection() as conn, conn.transaction():
                await require_fence(conn, fence)
                await conn.execute(
                    "UPDATE app_state.runs SET "
                    "lease_until=LEAST(active_deadline,clock_timestamp()+interval '60 "
                    "seconds') WHERE id=%s",
                    (fence.run_id,),
                )
            return True
        except DomainError:
            return False

    async def reserve(self, fence, kind):
        if kind not in LIMITS:
            raise ValueError("unknown_budget_kind")
        column, maximum = LIMITS[kind]
        error = None
        budget = None
        async with self.pool.connection() as conn, conn.transaction():
            row = await execution_row(conn, fence)
            if row[column] >= maximum:
                await terminal(conn, row, "FAILED", "run_budget_exhausted")
                error = DomainError("run_budget_exhausted", 409)
            else:
                # column is a closed server-side enum, never a model identifier.
                current = await (
                    await conn.execute(
                        f"UPDATE app_state.runs SET {column}={column}+1 "
                        "WHERE id=%s RETURNING model_used,query_used,repair_used,"
                        "GREATEST(0,EXTRACT(EPOCH FROM(active_deadline-clock_timestamp()))) "
                        "AS remaining_seconds",
                        (fence.run_id,),
                    )
                ).fetchone()
                seconds = float(current["remaining_seconds"])
                budget = RemainingBudget(**current, model_timeout_seconds=min(45, seconds))
        if error:
            raise error
        return budget

    async def pause(self, fence, query_id=None, review_payload=None):
        async with self.pool.connection() as conn, conn.transaction():
            await require_fence(conn, fence)
            current = await (
                await conn.execute(
                    """
                UPDATE app_state.runs SET state='WAITING_INPUT',
                  remaining_seconds=GREATEST(0,
                    TRUNC(EXTRACT(EPOCH FROM(active_deadline-clock_timestamp())),3)),
                  active_deadline=NULL,owner=NULL,lease_until=NULL,
                  waiting_until=clock_timestamp()+interval '24 hours',
                  waiting_query_id=%s,resume_confirmation_id=NULL,waiting_review=%s,
                  resume_payload=NULL,resume_review_id=NULL
                WHERE id=%s RETURNING *
                """,
                    (query_id, Jsonb(review_payload) if review_payload else None, fence.run_id),
                )
            ).fetchone()
            await emit(conn, current, "run.waiting_input")

    async def _resume_locked(self, conn, row):
        "Internal: caller owns authorization/conversation/run locks and bound approval."
        if conn.info.transaction_status != TransactionStatus.INTRANS:
            raise RuntimeError("resume_requires_transaction")
        current = await (
            await conn.execute(
                "UPDATE app_state.runs SET "
                "state='QUEUED',resume_requested=true,available_at=clock_timestamp"
                "(),queue_until=clock_timestamp()+%s*interval '1 "
                "second',waiting_until=NULL WHERE tenant_id=%s AND id=%s AND "
                "task_revision=%s AND state='WAITING_INPUT' AND "
                "waiting_until>clock_timestamp() AND remaining_seconds>0 "
                "RETURNING *",
                (QUEUE_SECONDS[row["scope"]], row["tenant_id"], row["id"], row["task_revision"]),
            )
        ).fetchone()
        if current is None:
            raise DomainError("confirmation_expired", 409)
        await emit(conn, current, "run.resumed")
        return current

    async def fail(self, fence, code, retriable=False):
        async with self.pool.connection() as conn, conn.transaction():
            row = await execution_row(conn, fence)
            backoff = 5 if row["attempts"] == 1 else 30
            remaining = (
                await (
                    await conn.execute(
                        "SELECT EXTRACT(EPOCH FROM(active_deadline-clock_timestamp())) AS "
                        "seconds FROM app_state.runs WHERE id=%s",
                        (fence.run_id,),
                    )
                ).fetchone()
            )["seconds"]
            if retriable and code in TRANSIENT and row["attempts"] < 3:
                if remaining <= backoff:
                    await terminal(conn, row, "TIMED_OUT", "execution_deadline")
                    return
                current = await (
                    await conn.execute(
                        "UPDATE app_state.runs SET "
                        "state='QUEUED',error_code=%s,owner=NULL,lease_until=NULL,availabl"
                        "e_at=clock_timestamp()+%s*interval '1 "
                        "second',queue_until=LEAST(active_deadline,clock_timestamp()+%s*in"
                        "terval '1 second') WHERE id=%s RETURNING *",
                        (code, backoff, QUEUE_SECONDS[row["scope"]], fence.run_id),
                    )
                ).fetchone()
                await emit(
                    conn,
                    current,
                    "run.retry_scheduled",
                    {"state": "QUEUED", "backoff_seconds": backoff},
                )
            else:
                await terminal(conn, row, "FAILED", code)

    async def finish(self, fence, report, *, actor=None, chart_specs=None):
        async with self.pool.connection() as conn, conn.transaction():
            row = await execution_row(conn, fence)
            if actor is not None:
                from insightpilot.reports.builder import ReportBuilder, preserve_safe_warnings
                from insightpilot.reports.models import ReportClaim
                from insightpilot.reports.repository import load_for_run

                evidence, flags, _ = await load_for_run(
                    conn, actor, fence, [item["evidence_id"] for item in report["evidence"]]
                )
                previous_warnings = report.get("warnings", [])
                rebuilt = ReportBuilder(chart_specs=chart_specs, query_flags=flags).build(
                    evidence, [ReportClaim.model_validate(claim) for claim in report["claims"]]
                )
                report = preserve_safe_warnings(rebuilt, previous_warnings).model_dump(mode="json")
            current = await (
                await conn.execute(
                    "UPDATE app_state.runs SET "
                    "state='SUCCEEDED',report=%s,owner=NULL,lease_until=NULL,ended_at="
                    "clock_timestamp(),error_code=NULL WHERE tenant_id=%s AND id=%s "
                    "AND topic_id=%s AND task_revision=%s AND execution_version=%s "
                    "AND owner=%s AND auth_version=%s AND state='RUNNING' "
                    "AND lease_until>clock_timestamp() AND active_deadline>clock_timestamp() "
                    "RETURNING *",
                    (
                        Jsonb(report),
                        fence.tenant_id,
                        fence.run_id,
                        fence.topic_id,
                        fence.task_revision,
                        fence.execution_version,
                        fence.owner,
                        fence.auth_version,
                    ),
                )
            ).fetchone()
            if current is None:
                raise DomainError("stale_execution", 409)
            await conn.execute(
                "INSERT INTO "
                "app_state.messages(id,tenant_id,conversation_id,topic_id,run_id,r"
                "ole,status,content) "
                "VALUES(%s,%s,%s,%s,%s,'assistant','completed',%s)",
                (
                    uuid4(),
                    row["tenant_id"],
                    row["conversation_id"],
                    row["topic_id"],
                    row["id"],
                    report["summary"],
                ),
            )
            await emit(conn, current, "run.succeeded")
