"Bounded persistent-state maintenance, independent of Redis notifications."

from dataclasses import dataclass

from insightpilot.runs.repository import lock_system_run, terminal


@dataclass(frozen=True)
class RecoveryStats:
    checked: int = 0
    terminated: int = 0


class RecoverySweep:
    def __init__(self, pool):
        self.pool = pool

    async def run_once(self):
        async with self.pool.connection() as conn:
            candidates = await (
                await conn.execute("""
                SELECT r.id FROM app_state.runs r
                JOIN app_state.users u ON u.tenant_id=r.tenant_id AND u.id=r.user_id
                JOIN app_state.conversations c ON c.tenant_id=r.tenant_id AND c.id=r.conversation_id
                WHERE r.state IN ('QUEUED','RUNNING','WAITING_INPUT') AND (
                  NOT u.enabled OR u.auth_version<>r.auth_version OR
                  (r.scope='INTERACTIVE' AND (r.epoch<>c.epoch OR r.topic_id<>c.active_topic_id)) OR
                  r.active_deadline<=clock_timestamp() OR
                  (r.state='QUEUED' AND r.queue_until<=clock_timestamp()) OR
                  (r.state='WAITING_INPUT' AND r.waiting_until<=clock_timestamp()) OR
                  (r.state='RUNNING' AND r.lease_until<=clock_timestamp() AND r.attempts>=3)
                ) ORDER BY r.created_at,r.id LIMIT 100
            """)
            ).fetchall()
        ended = 0
        for candidate in candidates:
            async with self.pool.connection() as conn, conn.transaction():
                user, conversation, row = await lock_system_run(conn, candidate["id"])
                if row is None or row["state"] not in ("QUEUED", "RUNNING", "WAITING_INPUT"):
                    continue
                now = (await (await conn.execute("SELECT clock_timestamp() AS now")).fetchone())[
                    "now"
                ]
                state, code, rev = None, None, False
                if not user["enabled"] or user["auth_version"] != row["auth_version"]:
                    state, code = "FAILED", "authorization_revoked"
                elif row["scope"] == "INTERACTIVE" and (
                    row["epoch"] != conversation["epoch"]
                    or row["topic_id"] != conversation["active_topic_id"]
                ):
                    state, code, rev = "SUPERSEDED", "foreground_replaced", True
                elif row["active_deadline"] and row["active_deadline"] <= now:
                    state, code = "TIMED_OUT", "execution_deadline"
                elif row["state"] == "QUEUED" and row["queue_until"] <= now:
                    state, code = "TIMED_OUT", "queue_deadline"
                elif (
                    row["state"] == "WAITING_INPUT"
                    and row["waiting_until"]
                    and row["waiting_until"] <= now
                ):
                    state, code = "TIMED_OUT", "confirmation_expired"
                elif (
                    row["state"] == "RUNNING"
                    and row["lease_until"]
                    and row["lease_until"] <= now
                    and row["attempts"] >= 3
                ):
                    state, code = "FAILED", "retry_exhausted"
                if state:
                    await terminal(conn, row, state, code, rev)
                    ended += 1
        return RecoveryStats(len(candidates), ended)
