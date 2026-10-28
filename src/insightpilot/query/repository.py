from psycopg.types.json import Jsonb

from insightpilot.auth.service import require_current
from insightpilot.catalog.models import Catalog
from insightpilot.errors import DomainError
from insightpilot.migrations import load_manifest
from insightpilot.query.models import EvidenceResult
from insightpilot.runs.repository import execution_row


async def authorized_scope(conn, actor, fence):
    await require_current(conn, actor, True)
    if actor.tenant_id != fence.tenant_id:
        raise DomainError("query_context_forbidden", 403)
    row = await execution_row(conn, fence)
    if row["user_id"] != actor.user_id:
        raise DomainError("run_not_found", 404)
    version = await (
        await conn.execute(
            "SELECT id,as_of FROM app_state.dataset_versions WHERE "
            "tenant_id=%s AND id=%s AND active FOR SHARE",
            (actor.tenant_id, row["dataset_version"]),
        )
    ).fetchone()
    if version is None:
        raise DomainError("dataset_unavailable", 403)
    return row, Catalog(dataset_version=version["id"], as_of=version["as_of"], **load_manifest())


async def ticket_for_run(conn, actor, fence, query_id, lock=False):
    row = await (
        await conn.execute(
            "SELECT * FROM app_state.query_tickets WHERE tenant_id=%s AND "
            "run_id=%s AND id=%s" + (" FOR UPDATE" if lock else ""),
            (actor.tenant_id, fence.run_id, query_id),
        )
    ).fetchone()
    if row is None:
        raise DomainError("query_not_found", 404)
    if row["task_revision"] != fence.task_revision or row["auth_version"] != actor.auth_version:
        raise DomainError("query_stale", 409)
    return row


async def existing_evidence(conn, actor, fence, query_id):
    row = await (
        await conn.execute(
            "SELECT content FROM app_state.query_evidence WHERE tenant_id=%s "
            "AND run_id=%s AND query_id=%s",
            (actor.tenant_id, fence.run_id, query_id),
        )
    ).fetchone()
    return EvidenceResult.model_validate(row["content"]) if row else None


async def save_evidence(conn, actor, fence, evidence):
    row = await (
        await conn.execute(
            "INSERT INTO "
            "app_state.query_evidence(id,tenant_id,run_id,task_revision,query_"
            "id,content,sha256) VALUES(%s,%s,%s,%s,%s,%s,%s) ON "
            "CONFLICT(tenant_id,run_id,query_id) DO NOTHING RETURNING content",
            (
                evidence.evidence_id,
                actor.tenant_id,
                fence.run_id,
                fence.task_revision,
                evidence.query_id,
                Jsonb(evidence.model_dump(mode="json")),
                evidence.sha256,
            ),
        )
    ).fetchone()
    return (
        EvidenceResult.model_validate(row["content"])
        if row
        else await existing_evidence(conn, actor, fence, evidence.query_id)
    )
