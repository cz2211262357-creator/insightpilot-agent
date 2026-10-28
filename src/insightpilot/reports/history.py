"Historical results require current identity, data visibility and SQL policy."

import hashlib
from uuid import UUID

from insightpilot.auth.service import require_current
from insightpilot.catalog.models import Catalog
from insightpilot.errors import DomainError
from insightpilot.migrations import load_manifest
from insightpilot.query.evidence import encoded
from insightpilot.query.models import EvidenceResult
from insightpilot.query.policy import SqlPolicy
from insightpilot.reports.builder import ReportBuilder, preserve_safe_warnings
from insightpilot.reports.completeness import query_is_partial
from insightpilot.reports.models import ReportClaim
from insightpilot.runs.service import own_run


async def catalog_for_report(conn, actor, row):
    version = await (
        await conn.execute(
            """
        SELECT id,as_of,catalog_version FROM app_state.dataset_versions
        WHERE tenant_id=%s AND id=%s AND active FOR SHARE
    """,
            (actor.tenant_id, row["dataset_version"]),
        )
    ).fetchone()
    if version is None:
        raise DomainError("dataset_unavailable", 403)
    manifest = load_manifest()
    if version["catalog_version"] != manifest["version"]:
        raise DomainError("catalog_changed", 403)
    return Catalog(dataset_version=version["id"], as_of=version["as_of"], **manifest)


async def visible_snapshot(conn, actor, row):
    from insightpilot.runs.service import snapshot

    row = dict(row)
    if row["report"] is not None:
        try:
            await catalog_for_report(conn, actor, row)
        except DomainError as error:
            row["report"] = None
            row["report_error_code"] = error.code
    row["has_report"] = row["report"] is not None
    return snapshot(row)


class HistoricalReports:
    def __init__(self, pool):
        self.pool = pool

    async def _load(self, conn, actor, row, catalog, identity):
        saved = await (
            await conn.execute(
                """
            SELECT e.content,t.sql,t.params,t.explain,t.catalog_version,t.policy_version
            FROM app_state.query_evidence e JOIN app_state.query_tickets t
              ON t.tenant_id=e.tenant_id AND t.run_id=e.run_id AND t.id=e.query_id
            WHERE e.tenant_id=%s AND e.run_id=%s AND e.task_revision=%s AND e.id=%s
        """,
                (actor.tenant_id, row["id"], row["task_revision"], UUID(str(identity))),
            )
        ).fetchone()
        if saved is None:
            raise DomainError("evidence_not_found", 404)
        evidence = EvidenceResult.model_validate(saved["content"])
        query = SqlPolicy().validate(saved["sql"], saved["params"], catalog)
        content = evidence.model_dump(mode="json")
        content.pop("evidence_id")
        content.pop("sha256")
        if (
            query.sql_hash != evidence.sql_hash
            or query.dataset_version != evidence.dataset_version
            or hashlib.sha256(encoded(content)).hexdigest() != evidence.sha256
        ):
            raise DomainError("evidence_stale", 403)
        return evidence, saved

    async def report(self, actor, run_id):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            row = await own_run(conn, actor, run_id)
            catalog = await catalog_for_report(conn, actor, row)
            if row["state"] != "SUCCEEDED" or row["report"] is None:
                raise DomainError("report_not_ready", 409)
            evidence, flags = [], {}
            for item in row["report"]["evidence"]:
                source, ticket = await self._load(conn, actor, row, catalog, item["evidence_id"])
                evidence.append(source)
                flags[str(source.evidence_id)] = {"limited": query_is_partial(ticket["sql"])}
            rebuilt = ReportBuilder(
                chart_specs=row["report"].get("chart_specs", {}), query_flags=flags
            ).build(
                evidence, [ReportClaim.model_validate(claim) for claim in row["report"]["claims"]]
            )
            return preserve_safe_warnings(rebuilt, row["report"].get("warnings", []))

    async def evidence(self, actor, run_id, identity):
        async with self.pool.connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            row = await own_run(conn, actor, run_id)
            catalog = await catalog_for_report(conn, actor, row)
            if row["state"] != "SUCCEEDED" or row["report"] is None:
                raise DomainError("report_not_ready", 409)
            if str(identity) not in {item["evidence_id"] for item in row["report"]["evidence"]}:
                raise DomainError("evidence_not_found", 404)
            evidence, ticket = await self._load(conn, actor, row, catalog, identity)
            return {
                "evidence": evidence,
                **{
                    key: ticket[key]
                    for key in ("sql", "params", "explain", "catalog_version", "policy_version")
                },
                "metric_definitions": [
                    metric for metric in catalog.metrics if metric.metric_id in evidence.metric_ids
                ],
            }
