"Explicit bound human approval; ordinary messages cannot call this transition."

import base64
import hashlib
import hmac
import secrets
from datetime import UTC
from uuid import uuid4

from psycopg.types.json import Jsonb

from insightpilot.auth.service import require_current
from insightpilot.errors import DomainError
from insightpilot.query.evidence import encoded
from insightpilot.runs.repository import RunRepository
from insightpilot.runs.service import own_conversation, own_run


def budget_snapshot(row):
    return {
        **{name: row[name] for name in ("model_used", "query_used", "repair_used")},
        "remaining_seconds": format(row["remaining_seconds"], ".3f"),
    }


def bound_token(key, row):
    material = {
        name: str(row[name])
        for name in (
            "id",
            "tenant_id",
            "run_id",
            "user_id",
            "task_revision",
            "auth_version",
            "query_id",
            "sql_hash",
            "checkpoint_digest",
        )
    }
    material["budget"] = row["budget"]
    material["expires_at"] = row["expires_at"].astimezone(UTC).isoformat()
    # Domain separation prevents this capability from being a MCP bearer token.
    derived = hmac.digest(
        key.get_secret_value().encode(), b"insightpilot-confirmation-v1", "sha256"
    )
    return (
        base64.urlsafe_b64encode(hmac.digest(derived, encoded(material), "sha256"))
        .decode()
        .rstrip("=")
    )


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


async def require_approval(conn, actor, fence, run, ticket, approval_id):
    if approval_id is None:
        raise DomainError("query_approval_required", 409)
    approved = await (
        await conn.execute(
            """
        SELECT * FROM app_state.confirmations WHERE tenant_id=%s AND run_id=%s
          AND query_id=%s AND id=%s AND user_id=%s AND consumed_at IS NOT NULL
        """,
            (actor.tenant_id, fence.run_id, ticket["id"], approval_id, actor.user_id),
        )
    ).fetchone()
    if (
        approved is None
        or approved["task_revision"] != fence.task_revision
        or approved["auth_version"] != actor.auth_version
        or approved["sql_hash"] != ticket["sql_hash"]
        or run["resume_confirmation_id"] != approval_id
    ):
        raise DomainError("query_approval_stale", 409)


class ConfirmationService:
    def __init__(self, pool, signing_key):
        if len(signing_key.get_secret_value()) < 32:
            raise ValueError("confirmation_key_missing")
        self.pool = pool
        self.signing_key = signing_key

    async def create(self, actor, run_id, query_id):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            pointer = await own_run(conn, actor, run_id)
            await own_conversation(conn, actor, pointer["conversation_id"], True)
            run = await own_run(conn, actor, run_id, True)
            if (
                run["state"] != "WAITING_INPUT"
                or not run["checkpoint_digest"]
                or run["waiting_query_id"] != query_id
            ):
                raise DomainError("confirmation_not_available", 409)
            ticket = await (
                await conn.execute(
                    "SELECT * FROM app_state.query_tickets WHERE tenant_id=%s AND "
                    "run_id=%s AND id=%s",
                    (actor.tenant_id, run_id, query_id),
                )
            ).fetchone()
            if ticket is None:
                raise DomainError("query_not_found", 404)
            if not ticket["requires_approval"]:
                raise DomainError("query_approval_not_required", 422)
            if (
                ticket["task_revision"] != run["task_revision"]
                or ticket["auth_version"] != actor.auth_version
                or run["auth_version"] != actor.auth_version
            ):
                raise DomainError("confirmation_stale", 409)
            row = await (
                await conn.execute(
                    """
                SELECT * FROM app_state.confirmations WHERE tenant_id=%s AND run_id=%s
                  AND task_revision=%s AND query_id=%s AND checkpoint_digest=%s
                """,
                    (
                        actor.tenant_id,
                        run_id,
                        run["task_revision"],
                        query_id,
                        run["checkpoint_digest"],
                    ),
                )
            ).fetchone()
            if row is None:
                row = {
                    "id": uuid4(),
                    "tenant_id": actor.tenant_id,
                    "run_id": run_id,
                    "user_id": actor.user_id,
                    "task_revision": run["task_revision"],
                    "auth_version": actor.auth_version,
                    "query_id": query_id,
                    "sql_hash": ticket["sql_hash"],
                    "checkpoint_digest": run["checkpoint_digest"],
                    "budget": budget_snapshot(run),
                    "expires_at": run["waiting_until"],
                }
                token = bound_token(self.signing_key, row)
                await conn.execute(
                    """
                    INSERT INTO app_state.confirmations(id,tenant_id,run_id,user_id,task_revision,
                      auth_version,query_id,sql_hash,checkpoint_digest,budget,token_hash,expires_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        row["id"],
                        actor.tenant_id,
                        run_id,
                        actor.user_id,
                        row["task_revision"],
                        actor.auth_version,
                        query_id,
                        ticket["sql_hash"],
                        row["checkpoint_digest"],
                        Jsonb(row["budget"]),
                        token_digest(token),
                        row["expires_at"],
                    ),
                )
            else:
                token = bound_token(self.signing_key, row)
            return {
                "confirmation_id": row["id"],
                "token": token,
                "run_id": run_id,
                "topic_id": run["topic_id"],
                "task_revision": run["task_revision"],
                "query_id": query_id,
                "sql": ticket["sql"],
                "params": ticket["params"],
                "explain": ticket["explain"],
                "sql_hash": ticket["sql_hash"],
                "budget": row["budget"],
                "expires_at": row["expires_at"],
                "catalog_version": ticket["catalog_version"],
            }

    async def pending(self, actor, run_id):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            run = await own_run(conn, actor, run_id)
            query_id = run["waiting_query_id"]
        if query_id is None:
            raise DomainError("confirmation_not_available", 409)
        return await self.create(actor, run_id, query_id)

    async def approve(
        self, actor, confirmation_id, token, idempotency_key=None, decision_hash=None
    ):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            pointer = await (
                await conn.execute(
                    "SELECT run_id FROM app_state.confirmations WHERE tenant_id=%s "
                    "AND user_id=%s AND id=%s",
                    (actor.tenant_id, actor.user_id, confirmation_id),
                )
            ).fetchone()
            if pointer is None:
                raise DomainError("confirmation_not_found", 404)
            run = await own_run(conn, actor, pointer["run_id"])
            await own_conversation(conn, actor, run["conversation_id"], True)
            run = await own_run(conn, actor, run["id"], True)
            row = await (
                await conn.execute(
                    "SELECT * FROM app_state.confirmations WHERE tenant_id=%s AND id=%s FOR UPDATE",
                    (actor.tenant_id, confirmation_id),
                )
            ).fetchone()
            if (
                not isinstance(token, str)
                or len(token) != 43
                or not secrets.compare_digest(token_digest(token), row["token_hash"])
            ):
                raise DomainError("confirmation_token_invalid", 409)
            if (
                row["auth_version"] != actor.auth_version
                or run["auth_version"] != actor.auth_version
                or run["task_revision"] != row["task_revision"]
            ):
                raise DomainError("confirmation_stale", 409)
            if row["consumed_at"] is not None:
                if idempotency_key is not None and (
                    row["idempotency_key"] != idempotency_key
                    or row["decision_hash"] != decision_hash
                ):
                    raise DomainError("confirmation_conflict", 409)
                return row["receipt"]
            now = (await (await conn.execute("SELECT clock_timestamp() AS now")).fetchone())["now"]
            if (
                run["state"] != "WAITING_INPUT"
                or run["task_revision"] != row["task_revision"]
                or run["waiting_query_id"] != row["query_id"]
                or run["checkpoint_digest"] != row["checkpoint_digest"]
                or row["expires_at"] <= now
                or budget_snapshot(run) != row["budget"]
            ):
                raise DomainError("confirmation_stale", 409)
            ticket = await (
                await conn.execute(
                    "SELECT sql_hash FROM app_state.query_tickets WHERE tenant_id=%s "
                    "AND run_id=%s AND id=%s",
                    (actor.tenant_id, run["id"], row["query_id"]),
                )
            ).fetchone()
            if ticket is None or ticket["sql_hash"] != row["sql_hash"]:
                raise DomainError("confirmation_stale", 409)
            receipt = {
                "accepted": True,
                "run_id": str(run["id"]),
                "task_revision": run["task_revision"],
                "confirmation_id": str(confirmation_id),
            }
            await conn.execute(
                "UPDATE app_state.confirmations SET "
                "consumed_at=clock_timestamp(),receipt=%s,idempotency_key=%s,decis"
                "ion_hash=%s WHERE id=%s",
                (Jsonb(receipt), idempotency_key, decision_hash, confirmation_id),
            )
            await conn.execute(
                "UPDATE app_state.runs SET resume_confirmation_id=%s,resume_payload=%s WHERE id=%s",
                (
                    confirmation_id,
                    Jsonb(
                        {
                            "kind": "query_approval",
                            "query_id": str(row["query_id"]),
                            "approval_id": str(confirmation_id),
                            "approved": True,
                        }
                    ),
                    run["id"],
                ),
            )
            await RunRepository(self.pool)._resume_locked(conn, run)
            return receipt
