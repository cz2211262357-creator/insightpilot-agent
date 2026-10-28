"Explicit local browser gate; uses existing Playwright/Edge, real owned services."

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

from tests.controlled_provider import ControlledProvider, narrative, plan, query
from tests.integration.test_agent_graph import agent_case as agent_case
from tests.integration.test_mcp_http import mcp_process as mcp_process
from tests.integration.test_run_races import scenario as scenario
from tests.support import PASSWORD, dsn


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def process(command, env, log, ready_url, cwd):
    with log.open("w", encoding="utf-8") as output:
        owned = subprocess.Popen(
            command,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            with httpx.Client(trust_env=False, timeout=0.3) as http:
                until = time.monotonic() + 25
                while True:
                    if owned.poll() is not None:
                        raise AssertionError(
                            "Owned browser dependency exited; inspect private fixture log"
                        )
                    try:
                        if http.get(ready_url).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > until:
                        raise AssertionError("Owned browser dependency failed readiness")
                    time.sleep(0.05)
            yield owned
        finally:
            if owned.poll() is None:
                owned.terminate()
                try:
                    owned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    owned.kill()
                    owned.wait(timeout=5)


def test_real_browser_owned_workbench(agent_case, workspace_root):
    dep, actors, (conversation, a, b), settings = agent_case
    with psycopg.connect(dsn(dep), autocommit=True) as conn:
        conn.execute("UPDATE app_state.topics SET title=%s WHERE id=%s", ("销售分析", a))
        conn.execute("UPDATE app_state.topics SET title=%s WHERE id=%s", ("售后趋势", b))
    project = Path(__file__).resolve().parents[2]
    node = Path(os.environ["IP_BROWSER_NODE"])
    playwright = Path(os.environ["IP_BROWSER_PLAYWRIGHT"])
    edge = Path(os.environ["IP_BROWSER_EXECUTABLE"])
    assert node.is_file() and playwright.is_file() and edge.is_file(), (
        "Explicit browser tools required; no skip"
    )
    folder = workspace_root / ".cache/insightpilot-verification" / ("browser-" + uuid4().hex)
    folder.mkdir()
    api_port, worker_port, web_port = port(), port(), port()
    origin = f"http://127.0.0.1:{web_port}"
    bar_plan = plan()
    bar_plan["steps"][0]["chart"] = {"kind": "bar", "x_column": "channel", "y_column": "paid_sales"}
    sql = (
        "SELECT c.channel_name AS channel,SUM(o.paid_amount) AS "
        "paid_sales FROM analysis.orders o JOIN analysis.channels c ON "
        "o.channel_id=c.channel_id WHERE o.status='paid' GROUP BY "
        "c.channel_name ORDER BY c.channel_name"
    )

    def description(body):
        result = narrative(body)
        result["claims"][0]["text_template"] = "该分组的实付销售额为 {value}"
        return result

    with (
        ControlledProvider([plan(True), plan(True), bar_plan, query(sql), description]) as provider,
        ExitStack() as stack,
    ):
        env = os.environ.copy()
        env.update(
            INSIGHTPILOT_WORKSPACE_ROOT=str(workspace_root),
            INSIGHTPILOT_MODEL_BASE_URL=provider.url,
            INSIGHTPILOT_MODEL_API_KEY="synthetic-browser-provider-key",
            INSIGHTPILOT_WEB_ORIGINS=origin,
            INSIGHTPILOT_DEV_API_TARGET=f"http://127.0.0.1:{api_port}",
            INSIGHTPILOT_BROWSER_PLAYWRIGHT=str(playwright),
            INSIGHTPILOT_BROWSER_EXECUTABLE=str(edge),
        )
        stack.enter_context(
            process(
                [
                    sys.executable,
                    "-m",
                    "insightpilot.cli",
                    "api",
                    "--workspace-root",
                    str(workspace_root),
                    "--port",
                    str(api_port),
                ],
                env,
                folder / "api.log",
                f"http://127.0.0.1:{api_port}/api/v1/health/ready",
                project,
            )
        )
        stack.enter_context(
            process(
                [
                    sys.executable,
                    "-m",
                    "insightpilot.cli",
                    "worker",
                    "--workspace-root",
                    str(workspace_root),
                    "--port",
                    str(worker_port),
                ],
                env,
                folder / "worker.log",
                f"http://127.0.0.1:{worker_port}/api/v1/health/ready",
                project,
            )
        )
        stack.enter_context(
            process(
                [
                    str(node),
                    str(project / "frontend/node_modules/vite/bin/vite.js"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                    "--strictPort",
                ],
                env,
                folder / "web.log",
                origin,
                project / "frontend",
            )
        )
        payload = {
            "origin": origin,
            "folder": str(folder),
            "tenant_id": str(actors["A_admin"].tenant_id),
            "email": "admin@a.synthetic.invalid",
            "password": PASSWORD,
            "conversation_id": str(conversation),
            "topic_a": str(a),
            "topic_b": str(b),
        }
        private = folder / "private-input.json"
        private.write_text(json.dumps(payload), encoding="utf-8")
        with (folder / "browser.log").open("w", encoding="utf-8") as output:
            result = subprocess.run(
                [str(node), str(project / "tests/browser/workbench.mjs"), str(private)],
                env=env,
                cwd=project,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        assert result.returncode == 0, "Browser flow failed; inspect owned private browser log"
        assert len(provider.requests) == 5
        proof = json.loads((folder / "proof.json").read_text(encoding="utf-8"))
        assert (
            proof["two_tab_conflict"] == 409
            and proof["old_task_waited"]
            and proof["bar_chart"]
            and proof["mobile_no_overflow"]
        )
        assert proof["page_errors"] == []
        (workspace_root / ".cache/insightpilot-verification/latest-browser.txt").write_text(
            str(folder), encoding="utf-8"
        )
