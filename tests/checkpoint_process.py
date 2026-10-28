"Disposable integration helper; authority travels via stdin, credentials via private env."

import asyncio
import json
import os
import sys
from uuid import UUID

from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from insightpilot.checkpoints import FencedPostgresSaver, checkpoint_scope
from insightpilot.runtime import new_event_loop
from insightpilot.types import Actor, Role, RunFence
from tests.checkpoint_graph import graph


async def main():
    payload = json.load(sys.stdin)
    actor = Actor(
        UUID(payload["tenant_id"]),
        UUID(payload["user_id"]),
        Role(payload["role"]),
        payload["auth_version"],
    )
    fence = RunFence(
        actor.tenant_id,
        UUID(payload["run_id"]),
        UUID(payload["topic_id"]),
        payload["task_revision"],
        payload["execution_version"],
        UUID(payload["owner"]),
        actor.auth_version,
    )
    async with AsyncConnectionPool(
        os.environ["INSIGHTPILOT_POSTGRES_CHECKPOINT_DSN"],
        open=False,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "prepare_threshold": 0,
            "options": "-c timezone=UTC -c search_path=graph_checkpoint,pg_catalog",
        },
    ) as pool:
        saver = FencedPostgresSaver(pool)
        with checkpoint_scope(actor, fence, payload["thread_id"]):
            compiled = graph(saver)
            config = {"configurable": {"thread_id": payload["thread_id"], "checkpoint_ns": ""}}
            incoming = (
                Command(resume={"approved": True, "approval_id": payload["approval_id"]})
                if payload["action"] == "resume"
                else {"counter": 0, "query_id": payload["query_id"], "approved": False}
            )
            result = await compiled.ainvoke(incoming, config)
            checkpoint = await saver.aget_tuple(config)
    print(
        json.dumps(
            {
                "counter": result["counter"],
                "approved": result["approved"],
                "interrupted": bool(result.get("__interrupt__")),
                "checkpoint_id": checkpoint.checkpoint["id"],
            }
        )
    )


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=new_event_loop) as runner:
        runner.run(main())
