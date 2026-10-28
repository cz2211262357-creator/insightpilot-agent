"python -m evals.run --mode controlled|online --report PATH."

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from evals.engine import PROJECT, evaluate_cases
from insightpilot.auth.service import AuthService
from insightpilot.config import Settings
from insightpilot.db import DatabasePools
from insightpilot.runtime import new_event_loop


async def online(root, report):
    settings = Settings.from_environment(root)
    accounts = json.loads((settings.data_dir / "accounts.json").read_text(encoding="utf-8"))
    account = next(
        (a for a in accounts if a["email"] == "analyst@a.synthetic.invalid"), accounts[0]
    )
    pools = DatabasePools(settings)
    await pools.open()
    token = None
    auth = AuthService(pools.postgres["state"], pools.redis)
    try:
        actor, token, _ = await auth.login(
            UUID(account["tenant_id"]), account["email"], account["password"]
        )
        cases = json.loads((PROJECT / "evals/gold_cases.json").read_text(encoding="utf-8"))
        return await evaluate_cases(
            settings, pools, actor, cases, mode="online", report_path=report
        )
    finally:
        if token:
            await auth.logout(token)
        await pools.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("controlled", "online"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--workspace-root", type=Path, default=os.environ.get("INSIGHTPILOT_WORKSPACE_ROOT")
    )
    args = parser.parse_args()
    if args.workspace_root is None:
        parser.error("Source scripts/env first or supply --workspace-root")
    root, report = args.workspace_root.resolve(), args.report.resolve()
    if not report.is_relative_to(root):
        parser.error("Report must remain inside WorkspaceRoot")
    os.environ["INSIGHTPILOT_WORKSPACE_ROOT"] = str(root)
    if args.mode == "controlled":
        import pytest

        outcome = pytest.main(
            [str(PROJECT / "tests/system/test_gold_agent.py"), "-q", "--tb=short"]
        )
        if outcome:
            raise SystemExit(int(outcome))
        original = root / ".cache/insightpilot-verification/controlled-gold.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        if original.resolve() != report:
            report.write_bytes(original.read_bytes())
        result = json.loads(report.read_text(encoding="utf-8"))
    else:
        with asyncio.Runner(loop_factory=new_event_loop) as runner:
            result = runner.run(online(root, report))
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
