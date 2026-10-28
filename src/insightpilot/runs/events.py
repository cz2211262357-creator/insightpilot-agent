"Bounded batches are authenticated in short transactions, never across a yield."

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from insightpilot.auth.service import AuthService, digest, require_current
from insightpilot.errors import DomainError
from insightpilot.runs.service import own_run

STAGES = {
    "bootstrap",
    "plan",
    "clarification",
    "generate",
    "validate",
    "repair",
    "cost_review",
    "execute",
    "describe",
}


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int
    run_id: UUID
    topic_id: UUID
    task_revision: int
    execution_version: int
    scope: Literal["INTERACTIVE", "BACKGROUND"]
    epoch: int
    kind: str
    payload: dict
    created_at: datetime


class EventBatch(BaseModel):
    events: list[RunEvent]
    cursor: int
    state: str


def public_payload(payload):
    output = {}
    for key in ("state", "status", "stage", "query_id", "evidence_id", "backoff_seconds"):
        value = payload.get(key)
        if value is not None and type(value) in (str, int, bool):
            output[key] = value
    if isinstance(output.get("stage"), str) and output["stage"] not in STAGES:
        output.pop("stage")
    if isinstance(payload.get("plan"), list):
        output["plan"] = [
            {
                "id": str(step.get("id", ""))[:8],
                "goal": str(step.get("goal", ""))[:240],
                "metric_ids": [str(value)[:64] for value in step.get("metric_ids", [])[:6]],
            }
            for step in payload["plan"][:4]
            if isinstance(step, dict)
        ]
    return output


class EventReader:
    def __init__(self, pool):
        self.pool = pool

    async def read(self, cookie, run_id, cursor=0):
        if type(cursor) is not int or cursor < 0:
            raise DomainError("event_cursor_invalid", 422)
        actor = await AuthService(self.pool).authenticate(cookie)
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            session = await (
                await conn.execute(
                    """
                SELECT user_id FROM app_state.sessions WHERE token_hash=%s AND tenant_id=%s
                AND user_id=%s AND auth_version=%s AND expires_at>clock_timestamp() FOR SHARE
            """,
                    (digest(cookie), actor.tenant_id, actor.user_id, actor.auth_version),
                )
            ).fetchone()
            if session is None:
                raise DomainError("identity_expired", 401)
            row = await own_run(conn, actor, run_id)
            active = await (
                await conn.execute(
                    """
                SELECT id FROM app_state.dataset_versions
                WHERE tenant_id=%s AND id=%s AND active FOR SHARE
            """,
                    (actor.tenant_id, row["dataset_version"]),
                )
            ).fetchone()
            if active is None:
                raise DomainError("dataset_unavailable", 403)
            events = await (
                await conn.execute(
                    """
                SELECT id AS seq,run_id,topic_id,epoch,task_revision,execution_version,
                  scope,kind,payload,created_at
                FROM app_state.events WHERE tenant_id=%s AND run_id=%s
                AND id>%s ORDER BY id LIMIT 100
            """,
                    (actor.tenant_id, UUID(str(run_id)), cursor),
                )
            ).fetchall()
        public = [
            RunEvent(**{**event, "payload": public_payload(event["payload"])}) for event in events
        ]
        return EventBatch(
            events=public, cursor=public[-1].seq if public else cursor, state=row["state"]
        )
