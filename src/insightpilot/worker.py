"Independent foreground/background lanes with short authority transactions."

import asyncio
import logging
from contextlib import suppress
from uuid import UUID, uuid4

import psycopg
from langgraph.types import Command
from langsmith import tracing_context
from psycopg_pool import PoolTimeout

from insightpilot.admission import Admission, enforce_execution_permit, execution_scope
from insightpilot.agent.graph import build_graph
from insightpilot.agent.model import AnalysisModel
from insightpilot.checkpoints import FencedPostgresSaver, checkpoint_scope
from insightpilot.errors import DomainError
from insightpilot.mcp_client import DataToolsClient
from insightpilot.runs.confirmations import ConfirmationService
from insightpilot.runs.recovery import RecoverySweep
from insightpilot.runs.repository import TRANSIENT, RunRepository
from insightpilot.types import Actor, Role

logger = logging.getLogger("insightpilot.worker")


class AnalysisWorker:
    def __init__(self, settings, pools, owner=None):
        self.settings, self.pools = settings, pools
        self.owner = owner or uuid4()
        self.state = pools.postgres["state"]
        self.repository = RunRepository(self.state)
        self.admission = Admission(pools.redis, self.state)

    async def run_once(self, execution_class):
        if execution_class not in ("INTERACTIVE", "BACKGROUND"):
            raise ValueError("execution_class_invalid")
        async with self.state.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                SELECT id FROM app_state.runs WHERE scope=%s AND
                ((state='QUEUED' AND available_at<=clock_timestamp()) OR
                 (state='RUNNING' AND lease_until<=clock_timestamp()-
                   CASE WHEN attempts=1 THEN interval '5 seconds' ELSE interval '30 seconds' END))
                ORDER BY created_at,id LIMIT 20
            """,
                    (execution_class,),
                )
            ).fetchall()
        if not rows:
            return False
        permit = await self.admission.acquire(execution_class, self.owner)
        if permit is None:
            return False
        try:
            for row in rows:
                fence = await self.repository.claim(row["id"], self.owner)
                if fence is not None:
                    await self._execute(fence, permit)
                    return True
            return False
        finally:
            with suppress(DomainError):
                await self.admission.release(permit)

    async def _execute(self, fence, permit):
        async with self.state.connection() as conn:
            row = await (
                await conn.execute(
                    """
                SELECT r.*,u.role,
                  GREATEST(0,EXTRACT(EPOCH FROM(r.active_deadline-clock_timestamp()))) AS seconds
                FROM app_state.runs r JOIN app_state.users u
                  ON u.tenant_id=r.tenant_id AND u.id=r.user_id
                WHERE r.id=%s
            """,
                    (fence.run_id,),
                )
            ).fetchone()
        actor = Actor(row["tenant_id"], row["user_id"], Role(row["role"]), row["auth_version"])
        model = AnalysisModel(self.settings, self.state, self.admission)
        graph = build_graph(
            model,
            DataToolsClient(self.settings, self.state),
            FencedPostgresSaver(self.pools.postgres["checkpoint"]),
        )
        cfg = {
            "configurable": {"thread_id": str(row["thread_id"]), "checkpoint_ns": ""},
            "recursion_limit": 64,
        }
        caller = asyncio.current_task()
        lost = asyncio.Event()

        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(10)
                    if not await self.admission.renew(permit) or not await self.repository.renew(
                        fence
                    ):
                        lost.set()
                        caller.cancel()
                        return
            except (DomainError, psycopg.Error, PoolTimeout):
                lost.set()
                caller.cancel()

        watcher = asyncio.create_task(heartbeat())
        try:
            async with execution_scope(self.admission, permit):
                with (
                    tracing_context(enabled=False),
                    checkpoint_scope(actor, fence, str(row["thread_id"])),
                ):
                    async with asyncio.timeout(float(row["seconds"])):
                        snapshot = await graph.aget_state(cfg)
                        interruptions = [
                            item for task in snapshot.tasks for item in task.interrupts
                        ]
                        if interruptions:
                            pending = interruptions[0].value
                            payload = row["resume_payload"]
                            bound = (
                                payload
                                and payload.get("kind") == pending.get("kind")
                                and (
                                    (
                                        pending.get("kind") == "query_approval"
                                        and payload.get("query_id") == pending.get("query_id")
                                    )
                                    or (
                                        pending.get("kind") == "clarification"
                                        and payload.get("nonce") == pending.get("nonce")
                                    )
                                )
                            )
                            result = (
                                await graph.ainvoke(Command(resume=payload), cfg)
                                if bound
                                else {"__interrupt__": interruptions}
                            )
                        elif snapshot.values and not snapshot.next:
                            result = snapshot.values
                        else:
                            result = await graph.ainvoke(
                                None if snapshot.values else {"question": row["question"]}, cfg
                            )
                        await enforce_execution_permit()
                        if result.get("__interrupt__"):
                            payload = result["__interrupt__"][0].value
                            await self.repository.pause(
                                fence,
                                query_id=UUID(payload["query_id"])
                                if payload["kind"] == "query_approval"
                                else None,
                                review_payload=payload,
                            )
                            await ConfirmationService(
                                self.state, self.settings.mcp_signing_key
                            ).issue(actor, fence.run_id)
                        elif (
                            result.get("status") == "complete" and result.get("report") is not None
                        ):
                            await self.repository.finish(
                                fence, result["report"], actor=actor, chart_specs=result["charts"]
                            )
                        else:
                            await self.repository.fail(
                                fence, result.get("error_code") or "analysis_incomplete"
                            )
        except asyncio.CancelledError:
            if not lost.is_set():
                raise
            await self._safe_fail(fence, "admission_lost", True)
        except TimeoutError:
            # The old fence has expired; maintenance materializes its deadline.
            await RecoverySweep(self.state).run_once()
        except DomainError as error:
            await self._safe_fail(fence, error.code, error.code in TRANSIENT)
        except RuntimeError as error:
            code = (
                error.args[0]
                if len(error.args) == 1 and isinstance(error.args[0], str)
                else "worker_internal_error"
            )
            if not code.isascii() or not code.replace("_", "").isalnum() or len(code) > 128:
                code = "worker_internal_error"
            await self._safe_fail(fence, code, code in TRANSIENT)
        except (psycopg.Error, PoolTimeout):
            # Leave durable state for bounded lease recovery; no fake success.
            logger.warning("database_unavailable")
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await model.aclose()

    async def _safe_fail(self, fence, code, retriable=False):
        try:
            await self.repository.fail(fence, code, retriable)
        except DomainError:
            # Cancel/revoke/supersede/another owner already controls the result.
            await RecoverySweep(self.state).run_once()
        except (psycopg.Error, PoolTimeout):
            logger.warning("database_unavailable")

    async def serve(self):
        async def lane(execution_class):
            while True:
                try:
                    handled = await self.run_once(execution_class)
                    if not handled:
                        await asyncio.sleep(0.25)
                except (DomainError, psycopg.Error, PoolTimeout):
                    logger.warning("worker_dependency_unavailable")
                    await asyncio.sleep(0.5)

        async def maintenance():
            while True:
                try:
                    await RecoverySweep(self.state).run_once()
                except (DomainError, psycopg.Error, PoolTimeout):
                    logger.warning("recovery_dependency_unavailable")
                await asyncio.sleep(1)

        async with asyncio.TaskGroup() as group:
            group.create_task(lane("INTERACTIVE"))
            group.create_task(lane("INTERACTIVE"))
            group.create_task(lane("BACKGROUND"))
            group.create_task(maintenance())
