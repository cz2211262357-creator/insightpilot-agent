"Typed, bound human decisions; no chat message can authorize a continuation."

import base64
import hashlib
import hmac
import secrets
from datetime import UTC
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, SecretStr

from insightpilot.auth.service import require_current
from insightpilot.errors import DomainError
from insightpilot.query.confirmations import ConfirmationService as QueryConfirmations
from insightpilot.query.confirmations import budget_snapshot, token_digest
from insightpilot.query.evidence import encoded
from insightpilot.runs.models import RunReceipt
from insightpilot.runs.repository import RunRepository
from insightpilot.runs.service import own_conversation, own_run


class ResumeKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: UUID
    token: SecretStr


def bound_review_token(key, row):
    material = {
        name: str(row[name])
        for name in (
            "id",
            "tenant_id",
            "run_id",
            "user_id",
            "task_revision",
            "auth_version",
            "checkpoint_digest",
        )
    }
    material.update(
        budget=row["budget"],
        payload=row["payload"],
        expires_at=row["expires_at"].astimezone(UTC).isoformat(),
    )
    derived = hmac.digest(key.get_secret_value().encode(), b"insightpilot-review-v1", "sha256")
    return (
        base64.urlsafe_b64encode(hmac.digest(derived, encoded(material), "sha256"))
        .decode()
        .rstrip("=")
    )


async def receipt_for_run(conn, actor, run_id):
    row = await (
        await conn.execute(
            """
        SELECT receipt FROM app_state.messages WHERE tenant_id=%s AND run_id=%s AND role='user'
    """,
            (actor.tenant_id, run_id),
        )
    ).fetchone()
    if row is None:
        raise DomainError("run_not_found", 404)
    return RunReceipt.model_validate(row["receipt"])


class ConfirmationService:
    def __init__(self, pool, key):
        self.pool, self.key = pool, key

    async def issue(self, actor, run_id):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            pointer = await own_run(conn, actor, run_id)
            await own_conversation(conn, actor, pointer["conversation_id"], True)
            run = await own_run(conn, actor, run_id, True)
            query_id = run["waiting_query_id"]
            if query_id is None:
                payload = run["waiting_review"]
                if (
                    run["state"] != "WAITING_INPUT"
                    or not run["checkpoint_digest"]
                    or not payload
                    or payload.get("kind") != "clarification"
                    or run["auth_version"] != actor.auth_version
                ):
                    raise DomainError("confirmation_not_available", 409)
                row = await (
                    await conn.execute(
                        """
                    SELECT * FROM app_state.agent_reviews WHERE tenant_id=%s AND run_id=%s
                    AND task_revision=%s AND checkpoint_digest=%s
                """,
                        (actor.tenant_id, run_id, run["task_revision"], run["checkpoint_digest"]),
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
                        "checkpoint_digest": run["checkpoint_digest"],
                        "budget": budget_snapshot(run),
                        "payload": payload,
                        "expires_at": run["waiting_until"],
                    }
                    token = bound_review_token(self.key, row)
                    await conn.execute(
                        """
                        INSERT INTO app_state.agent_reviews(id,tenant_id,run_id,user_id,
                        task_revision,auth_version,checkpoint_digest,budget,payload,token_hash,expires_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                        (
                            row["id"],
                            actor.tenant_id,
                            run_id,
                            actor.user_id,
                            run["task_revision"],
                            actor.auth_version,
                            run["checkpoint_digest"],
                            Jsonb(row["budget"]),
                            Jsonb(payload),
                            token_digest(token),
                            row["expires_at"],
                        ),
                    )
                else:
                    token = bound_review_token(self.key, row)
                return {
                    "kind": "clarification",
                    "confirmation_id": row["id"],
                    "token": token,
                    "run_id": run_id,
                    "topic_id": run["topic_id"],
                    "task_revision": run["task_revision"],
                    "question": payload["question"],
                    "budget": row["budget"],
                    "expires_at": row["expires_at"],
                }
        review = await QueryConfirmations(self.pool, self.key).create(actor, run_id, query_id)
        return {"kind": "query_approval", **review}

    async def resume(self, actor, run_id, confirmation_id, decision, key):
        digest = hashlib.sha256(encoded(decision)).hexdigest()
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            pointer = await own_run(conn, actor, run_id)
            await own_conversation(conn, actor, pointer["conversation_id"], True)
            run = await own_run(conn, actor, run_id, True)
            row = await (
                await conn.execute(
                    """
                SELECT * FROM app_state.agent_reviews WHERE tenant_id=%s AND run_id=%s
                AND id=%s AND user_id=%s FOR UPDATE
            """,
                    (actor.tenant_id, run_id, confirmation_id, actor.user_id),
                )
            ).fetchone()
            if row is not None:
                if not secrets.compare_digest(
                    token_digest(key.token.get_secret_value()), row["token_hash"]
                ):
                    raise DomainError("confirmation_token_invalid", 409)
                if (
                    row["auth_version"] != actor.auth_version
                    or run["auth_version"] != actor.auth_version
                    or run["task_revision"] != row["task_revision"]
                ):
                    raise DomainError("confirmation_stale", 409)
                if row["consumed_at"] is not None:
                    if (
                        row["idempotency_key"] != key.idempotency_key
                        or row["decision_hash"] != digest
                    ):
                        raise DomainError("confirmation_conflict", 409)
                    return RunReceipt.model_validate(row["receipt"])
                now = (await (await conn.execute("SELECT clock_timestamp() AS now")).fetchone())[
                    "now"
                ]
                if (
                    run["state"] != "WAITING_INPUT"
                    or row["expires_at"] <= now
                    or run["checkpoint_digest"] != row["checkpoint_digest"]
                    or budget_snapshot(run) != row["budget"]
                    or run["waiting_review"] != row["payload"]
                ):
                    raise DomainError("confirmation_stale", 409)
                if (
                    set(decision) != {"action", "answer"}
                    or decision.get("action") != "clarify"
                    or not isinstance(decision.get("answer"), str)
                    or not 1 <= len(decision["answer"].strip()) <= 2000
                    or "\x00" in decision["answer"]
                ):
                    raise DomainError("clarification_invalid", 422)
                receipt = await receipt_for_run(conn, actor, run_id)
                await conn.execute(
                    """
                    UPDATE app_state.agent_reviews SET consumed_at=clock_timestamp(),
                    idempotency_key=%s,decision_hash=%s,receipt=%s WHERE id=%s
                """,
                    (
                        key.idempotency_key,
                        digest,
                        Jsonb(receipt.model_dump(mode="json")),
                        confirmation_id,
                    ),
                )
                await conn.execute(
                    """
                    UPDATE app_state.runs SET resume_review_id=%s,resume_payload=%s WHERE id=%s
                """,
                    (
                        confirmation_id,
                        Jsonb(
                            {
                                "kind": "clarification",
                                "nonce": run["waiting_review"].get("nonce"),
                                "review_id": str(confirmation_id),
                                "answer": decision["answer"].strip(),
                            }
                        ),
                        run_id,
                    ),
                )
                await RunRepository(self.pool)._resume_locked(conn, run)
                return receipt
            # Preserve the same binding for cost approval. This pointer must match
            # the requested run before releasing locks and entering its service.
            cost = await (
                await conn.execute(
                    """
                SELECT id FROM app_state.confirmations WHERE tenant_id=%s AND run_id=%s
                AND user_id=%s AND id=%s
            """,
                    (actor.tenant_id, run_id, actor.user_id, confirmation_id),
                )
            ).fetchone()
            if cost is None:
                raise DomainError("confirmation_not_found", 404)
            if decision != {"action": "approve"}:
                raise DomainError("confirmation_decision_invalid", 422)
        await QueryConfirmations(self.pool, self.key).approve(
            actor, confirmation_id, key.token.get_secret_value(), key.idempotency_key, digest
        )
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            return await receipt_for_run(conn, actor, run_id)
