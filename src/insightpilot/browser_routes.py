import asyncio
import json
import time
from uuid import UUID

from fastapi import Query, Request
from fastapi.responses import StreamingResponse

from insightpilot.auth.routes import current_actor
from insightpilot.auth.service import require_current
from insightpilot.catalog.models import Catalog, MetricDefinition
from insightpilot.catalog.repository import CatalogRepository
from insightpilot.contracts import EvidenceView, MemberView, ReportView, RunPage, SystemView
from insightpilot.errors import DomainError
from insightpilot.reports.history import HistoricalReports, visible_snapshot
from insightpilot.runs.events import EventBatch, EventReader
from insightpilot.runs.service import RUN_PROJECTION, own_run


def install_browser(app):
    def pool(request):
        return request.app.state.pools.postgres["state"]

    @app.get("/api/v1/datasets", response_model=Catalog)
    async def datasets(request: Request):
        return await CatalogRepository(pool(request)).list_datasets(await current_actor(request))

    @app.get("/api/v1/metrics", response_model=list[MetricDefinition])
    async def metrics(request: Request):
        return (await datasets(request)).metrics

    @app.get("/api/v1/members", response_model=list[MemberView])
    async def members(request: Request):
        actor = await current_actor(request)
        if actor.role.value != "admin":
            raise DomainError("admin_required", 403)
        async with pool(request).connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            return await (
                await conn.execute(
                    """
                SELECT id AS user_id,email,display_name,role,enabled,auth_version
                FROM app_state.users
                WHERE tenant_id=%s ORDER BY display_name,id LIMIT 100
            """,
                    (actor.tenant_id,),
                )
            ).fetchall()

    @app.get("/api/v1/system", response_model=SystemView)
    async def system(request: Request):
        await current_actor(request)
        return {
            "model_name": request.app.state.settings.model_name,
            "data_kind": "synthetic",
            "model_concurrency": 2,
            "background_concurrency": 1,
            "model_calls": 8,
            "query_calls": 4,
            "repair_calls": 2,
            "interactive_seconds": 120,
            "background_seconds": 300,
        }

    @app.get("/api/v1/runs", response_model=RunPage)
    async def runs(
        request: Request, before: UUID | None = None, limit: int = Query(50, ge=1, le=100)
    ):
        actor = await current_actor(request)
        async with pool(request).connection() as conn, conn.transaction():
            await require_current(conn, actor, True)
            point = await own_run(conn, actor, before) if before else None
            rows = await (
                await conn.execute(
                    f"""
                SELECT {RUN_PROJECTION} FROM app_state.runs r WHERE r.tenant_id=%s AND r.user_id=%s
                AND (%s::timestamptz IS NULL OR (r.created_at,r.id)<(%s::timestamptz,%s::uuid))
                ORDER BY r.created_at DESC,r.id DESC LIMIT %s
            """,
                    (
                        actor.tenant_id,
                        actor.user_id,
                        point["created_at"] if point else None,
                        point["created_at"] if point else None,
                        before,
                        limit + 1,
                    ),
                )
            ).fetchall()
            items = [
                (await visible_snapshot(conn, actor, row)).model_copy(update={"report": None})
                for row in rows[:limit]
            ]
        return {"items": items, "next_cursor": items[-1].run_id if len(rows) > limit else None}

    @app.get("/api/v1/runs/{run_id}/report", response_model=ReportView)
    async def report(run_id: UUID, request: Request):
        return await HistoricalReports(pool(request)).report(await current_actor(request), run_id)

    @app.get("/api/v1/runs/{run_id}/evidence/{identity}", response_model=EvidenceView)
    async def evidence(run_id: UUID, identity: UUID, request: Request):
        return await HistoricalReports(pool(request)).evidence(
            await current_actor(request), run_id, identity
        )

    @app.get("/api/v1/runs/{run_id}/event-batch", response_model=EventBatch)
    async def event_batch(run_id: UUID, request: Request, cursor: int = Query(0, ge=0)):
        return await EventReader(pool(request)).read(
            request.cookies.get("ip_session"), run_id, cursor
        )

    @app.get(
        "/api/v1/runs/{run_id}/events",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
    )
    async def events(
        run_id: UUID, request: Request, cursor: int = Query(0, ge=0), once: bool = False
    ):
        reader = EventReader(pool(request))
        cookie = request.cookies.get("ip_session")
        first = await reader.read(cookie, run_id, cursor)

        async def stream():
            batch = first
            deadline = time.monotonic() + 25
            while True:
                for item in batch.events:
                    yield f"id: {item.seq}\nevent: run_event\ndata: {item.model_dump_json()}\n\n"
                cursor = batch.cursor
                if once or batch.state in {
                    "SUCCEEDED",
                    "FAILED",
                    "TIMED_OUT",
                    "CANCELLED",
                    "SUPERSEDED",
                }:
                    return
                if time.monotonic() >= deadline or await request.is_disconnected():
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(0.5)
                try:
                    batch = await reader.read(cookie, run_id, cursor)
                except DomainError as error:
                    event = "auth_expired" if error.status == 401 else "access_revoked"
                    yield "event: " + event + "\ndata: " + json.dumps({"code": error.code}) + "\n\n"
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"x-accel-buffering": "no", "cache-control": "no-store"},
        )
