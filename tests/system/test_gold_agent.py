import json
from pathlib import Path

from tests.integration.test_agent_graph import agent_case as agent_case
from tests.integration.test_mcp_http import mcp_process as mcp_process
from tests.integration.test_run_races import scenario as scenario
from tests.support import run
from tests.system.test_execution_recovery import opened


def test_twenty_gold_cases_traverse_worker_graph_mcp_sql_and_report(agent_case):
    from evals.engine import evaluate_cases

    _, actors, _, settings = agent_case
    cases = json.loads(
        (Path(__file__).parents[2] / "evals/gold_cases.json").read_text(encoding="utf-8")
    )

    async def check():
        async with opened(settings) as pools:
            result = await evaluate_cases(
                settings, pools, actors["A_admin"], cases, mode="controlled", drive_worker=True
            )
        assert result["mode"] == "controlled"
        assert result["summary"]["case_count"] == 20
        assert result["summary"]["evidence_match_count"] == 20, result["cases"]
        assert result["summary"]["clarification_required_asked_count"] == 8
        assert result["summary"]["clarification_required_correct_count"] == 8
        assert result["summary"]["strict_success_count"] == 20
        assert result["summary"]["query_count"] == 20
        assert result["summary"]["model_call_count"] == 68
        assert result["summary"]["reported_total_tokens"] == 3400
        assert all(
            c["terminal_state"] == "SUCCEEDED" and c["evidence_sha256"] for c in result["cases"]
        )
        assert len(set(c["run_id"] for c in result["cases"])) == 20
        path = settings.workspace_root / ".cache/insightpilot-verification/controlled-gold.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    run(check())
