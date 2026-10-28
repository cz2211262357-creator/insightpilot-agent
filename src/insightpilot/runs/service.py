"Conversation/version transitions and message idempotency share one transaction."

import hashlib
import json
from uuid import uuid4

from psycopg.types.json import Jsonb

from insightpilot.auth.service import require_current
from insightpilot.errors import DomainError
from insightpilot.runs.budget import ACTIVE_SECONDS, QUEUE_SECONDS
from insightpilot.runs.models import RunReceipt, RunSnapshot, Scope, TopicSnapshot
from insightpilot.runs.repository import ACTIVE, emit, terminal

RUN_PROJECTION = """r.*,
  COALESCE((SELECT MAX(e.id) FROM app_state.events e
    WHERE e.tenant_id=r.tenant_id AND e.run_id=r.id),0) AS snapshot_seq,
  COALESCE((SELECT e.payload->'plan' FROM app_state.events e
    WHERE e.tenant_id=r.tenant_id AND e.run_id=r.id AND e.kind='analysis.phase'
      AND e.payload ? 'plan' ORDER BY e.id DESC LIMIT 1),'[]'::jsonb) AS public_plan"""


def snapshot(row):
    return RunSnapshot(
        run_id=row["id"],
        **{
            name: row[name] for name in RunSnapshot.model_fields if name != "run_id" and name in row
        },
    )


def body_hash(topic, question, scope, supersedes=None):
    value = {
        "topic_id": str(topic),
        "question": question,
        "scope": str(scope),
        "supersedes_run_id": str(supersedes) if supersedes else None,
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_question(question):
    if (
        not isinstance(question, str)
        or not question.strip()
        or len(question) > 4000
        or "\x00" in question
    ):
        raise DomainError("invalid_question", 422)


async def own_conversation(conn, actor, conversation_id, lock=False):
    row = await (
        await conn.execute(
            "SELECT * FROM app_state.conversations WHERE tenant_id=%s AND "
            "user_id=%s AND id=%s" + (" FOR UPDATE" if lock else ""),
            (actor.tenant_id, actor.user_id, conversation_id),
        )
    ).fetchone()
    if row is None:
        raise DomainError("conversation_not_found", 404)
    return row


async def own_run(conn, actor, run_id, lock=False):
    row = await (
        await conn.execute(
            f"SELECT {RUN_PROJECTION} FROM app_state.runs r "
            "WHERE r.tenant_id=%s AND r.user_id=%s AND r.id=%s"
            + (" FOR UPDATE OF r" if lock else ""),
            (actor.tenant_id, actor.user_id, run_id),
        )
    ).fetchone()
    if row is None:
        raise DomainError("run_not_found", 404)
    return row


async def require_topic(conn, actor, conversation_id, topic_id):
    row = await (
        await conn.execute(
            "SELECT id FROM app_state.topics WHERE tenant_id=%s AND conversation_id=%s AND id=%s",
            (actor.tenant_id, conversation_id, topic_id),
        )
    ).fetchone()
    if row is None:
        raise DomainError("topic_not_found", 404)


async def original_receipt(conn, actor, conversation_id, client_message_id, fingerprint):
    row = await (
        await conn.execute(
            "SELECT body_hash,receipt FROM app_state.messages WHERE "
            "tenant_id=%s AND conversation_id=%s AND client_message_id=%s",
            (actor.tenant_id, conversation_id, client_message_id),
        )
    ).fetchone()
    if row:
        if row["body_hash"] != fingerprint:
            raise DomainError("idempotency_conflict", 409)
        return RunReceipt.model_validate(row["receipt"])
    return None


async def supersede_foreground(conn, conversation):
    rows = await (
        await conn.execute(
            "SELECT * FROM app_state.runs WHERE tenant_id=%s AND "
            "conversation_id=%s AND scope='INTERACTIVE' AND state=ANY(%s) "
            "ORDER BY id FOR UPDATE",
            (conversation["tenant_id"], conversation["id"], list(ACTIVE)),
        )
    ).fetchall()
    for row in rows:
        await terminal(conn, row, "SUPERSEDED", "foreground_replaced", True)


class RunService:
    def __init__(self, pool):
        self.pool = pool

    async def create_conversation(self, actor, title="新分析"):
        conversation_id, topic_id = uuid4(), uuid4()
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            await conn.execute(
                "INSERT INTO app_state.conversations(id,tenant_id,user_id) VALUES(%s,%s,%s)",
                (conversation_id, actor.tenant_id, actor.user_id),
            )
            await conn.execute(
                "INSERT INTO app_state.topics(id,tenant_id,conversation_id,title) "
                "VALUES(%s,%s,%s,%s)",
                (topic_id, actor.tenant_id, conversation_id, title),
            )
            await conn.execute(
                "UPDATE app_state.conversations SET active_topic_id=%s WHERE id=%s",
                (topic_id, conversation_id),
            )
        return TopicSnapshot(conversation_id=conversation_id, active_topic_id=topic_id, epoch=0)

    async def list_conversations(self, actor):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            return await (
                await conn.execute(
                    "SELECT id AS conversation_id,active_topic_id,epoch,created_at "
                    "FROM app_state.conversations WHERE tenant_id=%s AND user_id=%s "
                    "ORDER BY created_at DESC LIMIT 100",
                    (actor.tenant_id, actor.user_id),
                )
            ).fetchall()

    async def conversation(self, actor, conversation_id):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            row = await own_conversation(conn, actor, conversation_id)
            topics = await (
                await conn.execute(
                    "SELECT id AS topic_id,title,created_at FROM app_state.topics "
                    "WHERE tenant_id=%s AND conversation_id=%s ORDER BY created_at,id",
                    (actor.tenant_id, conversation_id),
                )
            ).fetchall()
        return {
            "conversation_id": conversation_id,
            "active_topic_id": row["active_topic_id"],
            "epoch": row["epoch"],
            "topics": topics,
        }

    async def create_topic(self, actor, conversation_id, title, expected_epoch):
        topic_id = uuid4()
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            conversation = await own_conversation(conn, actor, conversation_id, True)
            if conversation["epoch"] != expected_epoch:
                raise DomainError("conversation_conflict", 409)
            await conn.execute(
                "INSERT INTO app_state.topics(id,tenant_id,conversation_id,title) "
                "VALUES(%s,%s,%s,%s)",
                (topic_id, actor.tenant_id, conversation_id, title),
            )
            await supersede_foreground(conn, conversation)
            epoch = (
                await (
                    await conn.execute(
                        "UPDATE app_state.conversations SET "
                        "epoch=epoch+1,active_topic_id=%s WHERE id=%s RETURNING epoch",
                        (topic_id, conversation_id),
                    )
                ).fetchone()
            )["epoch"]
        return TopicSnapshot(conversation_id=conversation_id, active_topic_id=topic_id, epoch=epoch)

    async def switch_topic(self, actor, conversation_id, topic_id, expected_epoch):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            conversation = await own_conversation(conn, actor, conversation_id, True)
            if conversation["epoch"] != expected_epoch:
                raise DomainError("conversation_conflict", 409)
            await require_topic(conn, actor, conversation_id, topic_id)
            if conversation["active_topic_id"] == topic_id:
                return TopicSnapshot(
                    conversation_id=conversation_id,
                    active_topic_id=topic_id,
                    epoch=conversation["epoch"],
                )
            await supersede_foreground(conn, conversation)
            epoch = (
                await (
                    await conn.execute(
                        "UPDATE app_state.conversations SET "
                        "epoch=epoch+1,active_topic_id=%s WHERE id=%s RETURNING epoch",
                        (topic_id, conversation_id),
                    )
                ).fetchone()
            )["epoch"]
        return TopicSnapshot(conversation_id=conversation_id, active_topic_id=topic_id, epoch=epoch)

    async def _submit_locked(
        self,
        conn,
        actor,
        conversation,
        topic_id,
        client_message_id,
        question,
        scope,
        revision=1,
        supersedes=None,
        expected_epoch=None,
    ):
        fingerprint = body_hash(topic_id, question, scope, supersedes)
        existing = await original_receipt(
            conn, actor, conversation["id"], client_message_id, fingerprint
        )
        if existing:
            return existing
        if expected_epoch is not None and conversation["epoch"] != expected_epoch:
            raise DomainError("conversation_conflict", 409)
        await require_topic(conn, actor, conversation["id"], topic_id)
        dataset = await (
            await conn.execute(
                "SELECT id FROM app_state.dataset_versions WHERE tenant_id=%s AND active",
                (actor.tenant_id,),
            )
        ).fetchone()
        if dataset is None:
            raise DomainError("dataset_not_found", 404)
        await supersede_foreground(conn, conversation)
        epoch = (
            await (
                await conn.execute(
                    "UPDATE app_state.conversations SET "
                    "epoch=epoch+1,active_topic_id=%s WHERE id=%s RETURNING epoch",
                    (topic_id, conversation["id"]),
                )
            ).fetchone()
        )["epoch"]
        run_id, message_id = uuid4(), uuid4()
        current = await (
            await conn.execute(
                "INSERT INTO "
                "app_state.runs(id,tenant_id,conversation_id,topic_id,user_id,data"
                "set_version,auth_version,question,scope,epoch,task_revision,threa"
                "d_id,supersedes_run_id,queue_until,remaining_seconds) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+%"
                "s*interval '1 second',%s) RETURNING *",
                (
                    run_id,
                    actor.tenant_id,
                    conversation["id"],
                    topic_id,
                    actor.user_id,
                    dataset["id"],
                    actor.auth_version,
                    question,
                    scope.value,
                    epoch,
                    revision,
                    uuid4(),
                    supersedes,
                    QUEUE_SECONDS[scope.value],
                    ACTIVE_SECONDS[scope.value],
                ),
            )
        ).fetchone()
        receipt = RunReceipt(
            message_id=message_id,
            run_id=run_id,
            topic_id=topic_id,
            conversation_id=conversation["id"],
            task_revision=revision,
            epoch=epoch,
            scope=scope,
        )
        await conn.execute(
            "INSERT INTO "
            "app_state.messages(id,tenant_id,conversation_id,topic_id,run_id,c"
            "lient_message_id,body_hash,receipt,role,status,content) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'user','completed',%s)",
            (
                message_id,
                actor.tenant_id,
                conversation["id"],
                topic_id,
                run_id,
                client_message_id,
                fingerprint,
                Jsonb(receipt.model_dump(mode="json")),
                question,
            ),
        )
        await emit(conn, current, "run.queued")
        return receipt

    async def submit(
        self,
        actor,
        conversation_id,
        topic_id,
        client_message_id,
        question,
        scope,
        expected_epoch=None,
    ):
        validate_question(question)
        scope = Scope(scope)
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            conversation = await own_conversation(conn, actor, conversation_id, True)
            return await self._submit_locked(
                conn,
                actor,
                conversation,
                topic_id,
                client_message_id,
                question,
                scope,
                expected_epoch=expected_epoch,
            )

    async def get(self, actor, run_id):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            row = await own_run(conn, actor, run_id)
            from insightpilot.reports.history import visible_snapshot

            return await visible_snapshot(conn, actor, row)

    async def cancel(self, actor, run_id, expected_revision):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            pointer = await own_run(conn, actor, run_id)
            await own_conversation(conn, actor, pointer["conversation_id"], True)
            row = await own_run(conn, actor, run_id, True)
            if row["state"] == "CANCELLED" and row["task_revision"] == expected_revision + 1:
                from insightpilot.reports.history import visible_snapshot

                return await visible_snapshot(conn, actor, row)
            if row["task_revision"] != expected_revision or row["state"] not in ACTIVE:
                raise DomainError("run_conflict", 409)
            from insightpilot.reports.history import visible_snapshot

            await terminal(conn, row, "CANCELLED", "cancelled_by_user", True)
            return await visible_snapshot(conn, actor, await own_run(conn, actor, run_id))

    async def revise(self, actor, run_id, expected_revision, client_message_id, question):
        validate_question(question)
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            pointer = await own_run(conn, actor, run_id)
            conversation = await own_conversation(conn, actor, pointer["conversation_id"], True)
            fingerprint = body_hash(pointer["topic_id"], question, Scope(pointer["scope"]), run_id)
            existing = await original_receipt(
                conn, actor, conversation["id"], client_message_id, fingerprint
            )
            if existing:
                return existing
            row = await own_run(conn, actor, run_id, True)
            if row["task_revision"] != expected_revision or row["state"] not in ACTIVE:
                raise DomainError("run_conflict", 409)
            await terminal(conn, row, "SUPERSEDED", "task_revised", True)
            return await self._submit_locked(
                conn,
                actor,
                conversation,
                row["topic_id"],
                client_message_id,
                question,
                Scope(row["scope"]),
                expected_revision + 1,
                run_id,
            )

    async def topic_history(self, actor, topic_id):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            topic = await (
                await conn.execute(
                    "SELECT t.conversation_id FROM app_state.topics t JOIN "
                    "app_state.conversations c ON c.id=t.conversation_id AND "
                    "c.tenant_id=t.tenant_id WHERE t.tenant_id=%s AND t.id=%s AND "
                    "c.user_id=%s",
                    (actor.tenant_id, topic_id, actor.user_id),
                )
            ).fetchone()
            if topic is None:
                raise DomainError("topic_not_found", 404)
            messages = await (
                await conn.execute(
                    "SELECT id,run_id,role,status,content,created_at FROM "
                    "app_state.messages WHERE tenant_id=%s AND topic_id=%s ORDER BY "
                    "created_at DESC,id DESC LIMIT 200",
                    (actor.tenant_id, topic_id),
                )
            ).fetchall()
            rows = await (
                await conn.execute(
                    f"SELECT {RUN_PROJECTION} FROM app_state.runs r "
                    "WHERE r.tenant_id=%s AND r.topic_id=%s "
                    "ORDER BY r.created_at DESC LIMIT 100",
                    (actor.tenant_id, topic_id),
                )
            ).fetchall()
            from insightpilot.reports.history import visible_snapshot

            visible = [await visible_snapshot(conn, actor, row) for row in rows]
            by_run = {item.run_id: item for item in visible}
            message_runs = {
                message["run_id"] for message in messages if message["role"] == "assistant"
            }
            missing = message_runs - by_run.keys()
            if missing:
                extra = await (
                    await conn.execute(
                        "SELECT r.* FROM app_state.runs r WHERE r.tenant_id=%s "
                        "AND r.topic_id=%s AND r.user_id=%s AND r.id=ANY(%s)",
                        (actor.tenant_id, topic_id, actor.user_id, list(missing)),
                    )
                ).fetchall()
                for row in extra:
                    by_run[row["id"]] = await visible_snapshot(conn, actor, row)
            # Message and run pages have independent bounds. Authorize every
            # assistant message's own run, and hide anything we cannot verify.
            for message in messages:
                item = by_run.get(message["run_id"])
                if message["role"] == "assistant" and (
                    item is None or not item.has_report or item.report_error_code is not None
                ):
                    message["content"] = "当前数据版本不可访问，原报告内容已隐藏。"
        return {"messages": list(reversed(messages)), "runs": visible}
