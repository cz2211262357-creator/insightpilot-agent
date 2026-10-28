"Real-provider acceptance against local Compose; synthetic data only."

import argparse
import json
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import docker
import httpx
import psycopg
from psycopg.rows import dict_row

from evals.engine import source_identity, usage_totals
from insightpilot.config import Settings


class Acceptance:
    def __init__(self, root, output):
        self.settings = Settings.from_environment(root)
        self.output = output
        self.owned_runs = set()
        self.result = {
            "synthetic_data_only": True,
            "started_at": datetime.now(UTC).isoformat(),
            **source_identity(),
            "checks": [],
        }
        self.http = httpx.Client(
            base_url="http://127.0.0.1:8090",
            timeout=10,
            trust_env=False,
            headers={"origin": "http://127.0.0.1:8090"},
        )
        accounts = json.loads(
            (self.settings.data_dir / "accounts.json").read_text(encoding="utf-8")
        )
        account = next(a for a in accounts if a["email"] == "analyst@a.synthetic.invalid")
        self.request(
            "POST", "/auth/login", {k: account[k] for k in ("tenant_id", "email", "password")}
        )
        self.http.headers["x-csrf-token"] = self.http.cookies["ip_csrf"]

    def request(self, method, path, body=None):
        response = self.http.request(method, "/api/v1" + path, json=body)
        if not response.is_success:
            raise RuntimeError("acceptance_http_" + str(response.status_code))
        return response.json() if response.content else None

    def submit(self, question):
        topic = self.request("POST", "/conversations", {"title": "真实模型验收"})
        receipt = self.request(
            "POST",
            "/conversations/" + topic["conversation_id"] + "/messages",
            {
                "topic_id": topic["active_topic_id"],
                "expected_epoch": 0,
                "client_message_id": str(uuid4()),
                "question": question,
                "scope": "BACKGROUND",
            },
        )
        self.owned_runs.add(receipt["run_id"])
        return receipt

    def snapshot(self, run_id):
        return self.request("GET", "/runs/" + run_id)

    def wait(self, run_id, predicate, seconds=360):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            row = self.snapshot(run_id)
            if predicate(row):
                return row
            if row["state"] in ("FAILED", "CANCELLED", "SUPERSEDED", "TIMED_OUT"):
                raise RuntimeError("acceptance_run_" + (row["error_code"] or row["state"]))
            time.sleep(0.25)
        raise RuntimeError("acceptance_wait_expired")

    def resume(self, run_id, review, decision):
        return self.request(
            "POST",
            "/runs/" + run_id + "/resume",
            {
                "confirmation_id": review["confirmation_id"],
                "token": review["token"],
                "idempotency_key": str(uuid4()),
                "decision": decision,
            },
        )

    def record(self, name, run_id, **details):
        row = self.snapshot(run_id)
        with psycopg.connect(
            self.settings.postgres_state_dsn.get_secret_value(),
            autocommit=True,
            row_factory=dict_row,
        ) as conn:
            version = conn.execute(
                "SELECT execution_version,attempts FROM app_state.runs WHERE id=%s", (run_id,)
            ).fetchone()
            calls = conn.execute(
                "SELECT kind,model_name,provider_model,status,usage_status,input_tokens,"
                "output_tokens,total_tokens,error_code FROM app_state.model_calls "
                "WHERE run_id=%s "
                "ORDER BY reservation",
                (run_id,),
            ).fetchall()
        self.result["checks"].append(
            {
                "name": name,
                "run_id": run_id,
                "state": row["state"],
                "model_used": row["model_used"],
                "query_used": row["query_used"],
                **version,
                **details,
                "model_calls": calls,
                **usage_totals(calls),
            }
        )
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(
            json.dumps(self.result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps({"check": name, "state": row["state"], **details}, ensure_ascii=False),
            flush=True,
        )

    def clarification(self):
        run_id = self.submit("帮我看一下销售情况")["run_id"]
        self.wait(run_id, lambda r: r["state"] in ("WAITING_INPUT", "SUCCEEDED"))
        review = self.request("GET", "/runs/" + run_id + "/confirmation")
        if review["kind"] != "clarification":
            raise RuntimeError("clarification_not_triggered")
        self.resume(
            run_id,
            review,
            {"action": "clarify", "answer": "2026年8月已支付订单的实付销售额，按支付时间统计。"},
        )
        self.wait(run_id, lambda r: r["state"] == "SUCCEEDED")
        report = self.request("GET", "/runs/" + run_id + "/report")
        if not report["evidence"]:
            raise RuntimeError("clarification_evidence_missing")
        self.record("real_clarification_resume", run_id, passed=True)

    def costly(self):
        question = (
            "统计全部历史订单的三元组合数量，订单可以重复组合，包含所有订单状态，"
            "使用订单数量 order_count 指标，不限定日期。请将 analysis.orders 的三个副本 "
            "a、b、c 做 CROSS JOIN，用 COUNT(*) AS order_count 计数，不要改写为乘法。"
            "这是合成数据上的只读分析；如果执行计划成本高，请暂停并让我确认。"
        )
        run_id = self.submit(question)["run_id"]
        self.wait(run_id, lambda r: r["state"] in ("WAITING_INPUT", "SUCCEEDED"))
        review = self.request("GET", "/runs/" + run_id + "/confirmation")
        for _ in range(2):
            if review["kind"] != "clarification":
                break
            self.resume(
                run_id,
                review,
                {
                    "action": "clarify",
                    "answer": (
                        "我理解三重组合可能产生高成本，同意先生成计划并评估 SQL；"
                        "执行前仍必须等待单独的高成本审批。请保持三表 CROSS JOIN，"
                        "不要改写为乘法。"
                    ),
                },
            )
            self.wait(run_id, lambda r: r["state"] in ("WAITING_INPUT", "SUCCEEDED"))
            review = self.request("GET", "/runs/" + run_id + "/confirmation")
        if review["kind"] != "query_approval" or not review["explain"]["approval_reasons"]:
            raise RuntimeError("cost_gate_not_triggered")
        before = self.snapshot(run_id)
        self.resume(run_id, review, {"action": "approve"})
        row = self.wait(run_id, lambda r: r["state"] == "SUCCEEDED")
        report = self.request("GET", "/runs/" + run_id + "/report")
        values = [
            value
            for item in report["evidence"]
            for result in item["rows"]
            for value in result.values()
        ]
        if 96**3 not in values or row["query_used"] != 1:
            raise RuntimeError("cost_result_mismatch")
        self.record(
            "real_cost_confirmation",
            run_id,
            passed=True,
            explain=review["explain"],
            models_before_confirmation=before["model_used"],
            hard_query_seconds=3,
        )

    def cancel(self):
        run_id = self.submit(
            "统计2026年8月按渠道、地区和商品类别的实付销售额，分别生成分析和图表。"
        )["run_id"]
        self.wait(run_id, lambda r: r["state"] == "RUNNING" and r["model_used"] >= 1)
        row = self.snapshot(run_id)
        self.request(
            "POST", "/runs/" + run_id + "/cancel", {"expected_revision": row["task_revision"]}
        )
        # Observe a completed/failed actual provider attempt, bounded by call timeout.
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            with psycopg.connect(
                self.settings.postgres_state_dsn.get_secret_value(), autocommit=True
            ) as conn:
                active = conn.execute(
                    "SELECT count(*) FROM app_state.model_calls WHERE run_id=%s AND "
                    "status='STARTED'",
                    (run_id,),
                ).fetchone()[0]
            if active == 0:
                break
            time.sleep(0.5)
        after = self.snapshot(run_id)
        if after["state"] != "CANCELLED" or after["has_report"]:
            raise RuntimeError("cancel_published_result")
        self.record("real_call_cancellation", run_id, passed=True, late_report=False)

    def recover(self):
        run_id = self.submit("统计2026年8月已支付订单的实付销售额，金额按支付时间统计。")["run_id"]
        self.wait(run_id, lambda r: r["state"] == "RUNNING" and r["model_used"] >= 1)
        with psycopg.connect(
            self.settings.postgres_state_dsn.get_secret_value(), autocommit=True
        ) as conn:
            active = [
                str(r[0])
                for r in conn.execute(
                    "SELECT id FROM app_state.runs WHERE state IN ('QUEUED','RUNNING')"
                ).fetchall()
            ]
        if active != [run_id]:
            raise RuntimeError("recovery_requires_only_owned_active_run")
        with closing(docker.from_env()) as client:
            workers = client.containers.list(
                filters={
                    "label": [
                        "com.docker.compose.project=insightpilot",
                        "com.docker.compose.service=worker",
                    ]
                }
            )
            if len(workers) != 1:
                raise RuntimeError("recovery_worker_not_unique")
            worker = workers[0]
            worker.reload()
            if worker.labels.get("com.docker.compose.project") != "insightpilot":
                raise RuntimeError("recovery_worker_scope")
            try:
                worker.kill(signal="SIGKILL")
            finally:
                worker.start()
        row = self.wait(run_id, lambda r: r["state"] == "SUCCEEDED", seconds=390)
        if row["execution_version"] < 2 or row["attempts"] < 2:
            raise RuntimeError("recovery_takeover_not_hit")
        self.record(
            "real_worker_kill_recovery",
            run_id,
            passed=True,
            fault="SIGKILL after an actual provider reservation; new Worker waits for lease expiry",
        )

    def close(self):
        try:
            for run_id in self.owned_runs:
                try:
                    row = self.snapshot(run_id)
                    if row["state"] in ("QUEUED", "RUNNING", "WAITING_INPUT"):
                        self.request(
                            "POST",
                            "/runs/" + run_id + "/cancel",
                            {"expected_revision": row["task_revision"]},
                        )
                except (RuntimeError, httpx.HTTPError):
                    # Only this acceptance's IDs are touched; durable state is
                    # authoritative if the service is temporarily unavailable.
                    pass
            self.request("POST", "/auth/logout")
        finally:
            self.http.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--check", choices=("all", "clarification", "costly", "cancel", "recover"), default="all"
    )
    args = parser.parse_args()
    if not args.report.resolve().is_relative_to(args.workspace_root.resolve()):
        parser.error("Report must remain inside WorkspaceRoot")
    acceptance = Acceptance(args.workspace_root, args.report)
    try:
        for name in (
            ("clarification", "costly", "cancel", "recover")
            if args.check == "all"
            else (args.check,)
        ):
            getattr(acceptance, name)()
    finally:
        acceptance.close()


if __name__ == "__main__":
    main()
