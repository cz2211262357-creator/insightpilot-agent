"Reload and validate current, scoped evidence before publication."

import hashlib
from uuid import UUID

from insightpilot.errors import DomainError
from insightpilot.query.evidence import encoded
from insightpilot.query.models import EvidenceResult
from insightpilot.query.policy import SqlPolicy
from insightpilot.query.repository import authorized_scope, ticket_for_run
from insightpilot.reports.completeness import query_is_partial


async def load_for_run(conn, actor, fence, identities):
    _, catalog = await authorized_scope(conn, actor, fence)
    results, flags = [], {}
    for identity in identities:
        row = await (
            await conn.execute(
                """
            SELECT content FROM app_state.query_evidence WHERE tenant_id=%s AND run_id=%s
            AND task_revision=%s AND id=%s
        """,
                (actor.tenant_id, fence.run_id, fence.task_revision, UUID(str(identity))),
            )
        ).fetchone()
        if row is None:
            raise DomainError("report_evidence_unknown", 422)
        item = EvidenceResult.model_validate(row["content"])
        ticket = await ticket_for_run(conn, actor, fence, item.query_id)
        query = SqlPolicy().validate(ticket["sql"], ticket["params"], catalog)
        content = item.model_dump(mode="json")
        content.pop("evidence_id")
        content.pop("sha256")
        if (
            hashlib.sha256(encoded(content)).hexdigest() != item.sha256
            or query.sql_hash != item.sql_hash
            or query.dataset_version != item.dataset_version
        ):
            raise DomainError("report_evidence_stale", 422)
        flags[str(item.evidence_id)] = {"limited": query_is_partial(ticket["sql"])}
        results.append(item)
    return results, flags, catalog
