"Owned system-test child. Fault hooks exist only in tests, never production."

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from insightpilot.checkpoints import FencedPostgresSaver, current_checkpoint_scope
from insightpilot.config import Settings
from insightpilot.db import DatabasePools
from insightpilot.runtime import new_event_loop
from insightpilot.worker import AnalysisWorker


def await_file(path):
    deadline = time.monotonic() + 120
    while not path.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("owned_test_gate_expired")
        time.sleep(0.01)


async def main(args):
    root = Path(args.root)
    settings = Settings.from_environment(root)
    pools = DatabasePools(settings)
    await pools.open()
    worker = AnalysisWorker(settings, pools)
    observations = {
        "pid": os.getpid(),
        "owner": str(worker.owner),
        "fault_seen": False,
        "post_fault_attempts": 0,
        "post_fault_commits": 0,
    }
    original = FencedPostgresSaver.aput_writes

    async def writes(saver, config, values, task_id, task_path=""):
        pairs = list(values)
        if args.fault and any(key == "evidence_ids" and value for key, value in pairs):
            if not observations["fault_seen"]:
                observations["fault_seen"] = True
                scope = current_checkpoint_scope()
                await asyncio.to_thread(
                    Path(args.fault).write_text,
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "owner": str(scope.fence.owner),
                            "run_id": str(scope.fence.run_id),
                            "execution_version": scope.fence.execution_version,
                        }
                    ),
                    encoding="utf-8",
                )
                await asyncio.to_thread(await_file, Path(args.release))
            observations["post_fault_attempts"] += 1
            result = await original(saver, config, pairs, task_id, task_path)
            observations["post_fault_commits"] += 1
            return result
        return await original(saver, config, pairs, task_id, task_path)

    if args.fault:
        FencedPostgresSaver.aput_writes = writes
    try:
        await asyncio.to_thread(Path(args.ready).write_text, str(os.getpid()), encoding="utf-8")
        await asyncio.to_thread(await_file, Path(args.start))
        observations["handled"] = await worker.run_once(args.scope)
        async with pools.postgres["state"].connection() as conn:
            row = await (
                await conn.execute(
                    """
                SELECT state,task_revision,execution_version,attempts,model_used,query_used,
                  (SELECT COUNT(*) FROM app_state.messages m
                    WHERE m.run_id=r.id AND m.role='assistant') AS reports
                FROM app_state.runs r WHERE id=%s
            """,
                    (args.run_id,),
                )
            ).fetchone()
        observations["run"] = dict(row)
        await asyncio.to_thread(
            Path(args.result).write_text, json.dumps(observations), encoding="utf-8"
        )
    finally:
        FencedPostgresSaver.aput_writes = original
        await pools.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("root", "result", "ready", "start", "scope", "run-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--fault")
    parser.add_argument("--release")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    files = [
        Path(value).resolve()
        for value in (args.result, args.ready, args.start, args.fault, args.release)
        if value
    ]
    assert all(path.is_relative_to(root / ".cache/insightpilot-verification") for path in files)
    with asyncio.Runner(loop_factory=new_event_loop) as runner:
        runner.run(main(args))
