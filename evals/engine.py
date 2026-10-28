"Whole-Agent evaluation with independent RLS-scoped reference queries."

import asyncio
import hashlib
import itertools
import json
import math
import re
import shutil
import subprocess
import time
from collections import Counter
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import uuid4

from insightpilot.catalog.repository import CatalogRepository
from insightpilot.query.evidence import TYPE_NAMES, scalar
from insightpilot.reports.history import HistoricalReports
from insightpilot.runs.confirmations import ConfirmationService, ResumeKey
from insightpilot.runs.models import Scope
from insightpilot.runs.service import RunService
from insightpilot.worker import AnalysisWorker

PROJECT = Path(__file__).resolve().parents[1]


def normalized(value, kind):
    if value is None:
        return ("null", "")
    if kind in ("decimal", "integer"):
        number = Decimal(str(value)).quantize(Decimal(".000001"), rounding=ROUND_HALF_UP)
        return ("number", format(number.normalize(), "f"))
    if kind in ("date", "local_datetime"):
        return ("date", str(value).replace(" ", "T").removesuffix("T00:00:00"))
    return (kind, str(value))


def same_result(expected, actual):
    "Aliases/order may differ; preserve duplicate rows and dimension-value relationships."
    if actual.get("truncated") or len(expected["rows"]) != len(actual["rows"]):
        return False
    columns = expected["columns"]
    if len(columns) != len(actual["columns"]) or len(columns) > 6:
        return False
    reference = Counter(
        tuple(normalized(row[c], expected["types"][c]) for c in columns) for row in expected["rows"]
    )
    for permutation in itertools.permutations(actual["columns"]):
        candidate = Counter(
            tuple(normalized(row[c], actual["types"][c]) for c in permutation)
            for row in actual["rows"]
        )
        if candidate == reference:
            return True
    return False


def usage_totals(calls):
    result = {
        "complete_usage": all(
            all(c.get(k) is not None for k in ("input_tokens", "output_tokens", "total_tokens"))
            for c in calls
        )
    }
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        known = sum(c[field] for c in calls if c.get(field) is not None)
        result["known_" + field] = known
        result["reported_" + field] = (
            known if all(c.get(field) is not None for c in calls) else None
        )
    return result


def source_identity():
    git = shutil.which("git")
    if not git and Path("C:/Program Files/Git/bin/git.exe").is_file():
        git = "C:/Program Files/Git/bin/git.exe"
    commit, dirty = None, None
    if git:
        revision = subprocess.run(
            [git, "-C", str(PROJECT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if revision.returncode == 0:
            commit = revision.stdout.strip()
            status = subprocess.run(
                [git, "-C", str(PROJECT), "status", "--porcelain", "--", "."],
                capture_output=True,
                text=True,
                check=False,
            )
            dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    digest = hashlib.sha256()
    for directory in ("src", "migrations", "fixtures", "evals"):
        for file in sorted((PROJECT / directory).rglob("*")):
            if (
                file.is_file()
                and "__pycache__" not in file.parts
                and file.suffix in (".py", ".sql", ".json")
            ):
                digest.update(file.relative_to(PROJECT).as_posix().encode())
                digest.update(file.read_bytes())
    return {"source_commit": commit, "source_dirty": dirty, "source_sha256": digest.hexdigest()}


async def reference_query(pool, actor, version, case):
    # This SQL was written before model evaluation and is never sent to the online model.
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION READ ONLY")
        await conn.execute("SET LOCAL statement_timeout='3s'")
        await conn.execute(
            "SELECT set_config('insightpilot.tenant_id',%s,true),"
            "set_config('insightpilot.dataset_version',%s,true)",
            (str(actor.tenant_id), str(version)),
        )
        cursor = await conn.execute(case["sql"], case["params"])
        columns = [c.name for c in cursor.description]
        types = {c.name: TYPE_NAMES.get(c.type_code, "unsupported") for c in cursor.description}
        rows = await cursor.fetchmany(1001)
        if len(rows) > 1000:
            raise ValueError("gold_reference_limit")
        return {
            "columns": columns,
            "types": types,
            "rows": [{key: scalar(value) for key, value in row.items()} for row in rows],
        }


def scripted_provider(case):
    # Controlled mode is explicitly an engineering fixture, never live-model accuracy.
    from tests.controlled_provider import ControlledProvider, plan

    ready = {
        "needs_clarification": False,
        "clarification_question": None,
        "metric_ids": case["metric_ids"],
        "steps": [
            {
                "id": "s1",
                "goal": case["question"],
                "metric_ids": case["metric_ids"],
                "result_kind": "aggregate",
                "chart": {"kind": "table"},
            }
        ],
    }

    def describe(body):
        cells = json.loads(body["messages"][-1]["content"])["input"]["cells"]
        return {
            "claims": [
                {
                    "evidence_id": c["evidence_id"],
                    "column": c["column"],
                    "row_index": c["row_index"],
                    "operation": "cell",
                    "value": c["value"],
                    "text_template": "结果为 {value}",
                }
                for c in cells[:12]
            ]
        }

    responses = ([plan(True)] if case["requires_clarification"] else []) + [
        ready,
        {"sql": re.sub(r"%\((\w+)\)s", r":\1", case["sql"]), "params": case["params"]},
        describe,
    ]
    return ControlledProvider(responses)


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(cases):
    calls = [call for case in cases for call in case["model_calls"]]
    durations = [case["elapsed_seconds"] for case in cases]
    return {
        "case_count": len(cases),
        "succeeded_count": sum(c["terminal_state"] == "SUCCEEDED" for c in cases),
        "evidence_match_count": sum(c["evidence_match"] for c in cases),
        "metric_match_count": sum(c["metric_match"] for c in cases),
        "clarification_required_count": sum(c["requires_clarification"] for c in cases),
        "clarification_required_asked_count": sum(
            c["requires_clarification"] and c["clarification_count"] > 0 for c in cases
        ),
        "clarification_required_correct_count": sum(
            c["requires_clarification"]
            and c["clarification_count"] > 0
            and c["terminal_state"] == "SUCCEEDED"
            and c["evidence_match"]
            and c["metric_match"]
            for c in cases
        ),
        "strict_success_count": sum(
            c["terminal_state"] == "SUCCEEDED"
            and c["evidence_match"]
            and c["metric_match"]
            and (not c["requires_clarification"] or c["clarification_count"] > 0)
            for c in cases
        ),
        "query_count": sum(c["query_used"] for c in cases),
        "model_call_count": len(calls),
        "latency_p50_seconds": percentile(durations, 0.5),
        "latency_p95_seconds": percentile(durations, 0.95),
        "failure_reasons": dict(Counter(c["failure_reason"] for c in cases if c["failure_reason"])),
        **usage_totals(calls),
    }


def save_report(report, path):
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)


async def evaluate_cases(
    settings, pools, actor, cases, *, mode, drive_worker=False, report_path=None
):
    if mode not in ("online", "controlled") or any(c.get("synthetic") is not True for c in cases):
        raise ValueError("evaluation_requires_synthetic_cases")
    if mode == "controlled" and not drive_worker:
        raise ValueError("controlled_requires_owned_worker")
    state = pools.postgres["state"]
    service = RunService(state)
    catalog = await CatalogRepository(state).list_datasets(actor)
    async with state.connection() as conn:
        version = await (
            await conn.execute(
                "SELECT seed,sha256 FROM app_state.dataset_versions WHERE "
                "tenant_id=%s AND id=%s AND active",
                (actor.tenant_id, catalog.dataset_version),
            )
        ).fetchone()
    if version is None or version["seed"] != 20261025:
        raise ValueError("evaluation_dataset_not_preregistered_synthetic")
    report = {
        "format_version": 1,
        "mode": mode,
        "synthetic_data_only": True,
        "started_at": datetime.now(UTC).isoformat(),
        **source_identity(),
        "requested_model": settings.model_name,
        "dataset_version": str(catalog.dataset_version),
        "dataset_sha256": version["sha256"],
        "dataset_seed": version["seed"],
        "schema_version": 10,
        "metric_version": catalog.version,
        "gold_sha256": hashlib.sha256(
            json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "protocol": "20 synthetic cases; full graph/MCP/SQL/report; no gold SQL sent "
        "online; aliases and row order ignored; numeric comparison 6 "
        "decimals; null differs from zero; nearest-rank p50/p95 includes "
        "queue and scripted confirmation",
        "cases": [],
    }
    for case in cases:
        expected = await reference_query(
            pools.postgres["query"], actor, catalog.dataset_version, case
        )
        context = scripted_provider(case) if mode == "controlled" else nullcontext(None)
        with context as provider:
            configured = (
                settings.model_copy(update={"model_base_url": provider.url})
                if provider
                else settings
            )
            topic = await service.create_conversation(actor, "合成数据评测 " + case["id"])
            started = time.monotonic()
            receipt = await service.submit(
                actor,
                topic.conversation_id,
                topic.active_topic_id,
                uuid4(),
                case["question"],
                Scope.BACKGROUND,
                expected_epoch=0,
            )
            worker = AnalysisWorker(configured, pools) if drive_worker else None
            reviews, failure = [], None
            while True:
                if worker:
                    await worker.run_once("BACKGROUND")
                row = await service.get(actor, receipt.run_id)
                if row.state == "WAITING_INPUT":
                    review = await ConfirmationService(state, settings.mcp_signing_key).issue(
                        actor, receipt.run_id
                    )
                    if review["kind"] != "clarification" or len(reviews) >= 2:
                        failure = (
                            "unplanned_confirmation"
                            if review["kind"] != "clarification"
                            else "repeated_clarification"
                        )
                        await service.cancel(actor, receipt.run_id, row.task_revision)
                        row = await service.get(actor, receipt.run_id)
                        break
                    reviews.append(
                        {
                            "kind": review["kind"],
                            "question": review.get("question"),
                            "answer": case["clarification_answer"],
                        }
                    )
                    await ConfirmationService(state, settings.mcp_signing_key).resume(
                        actor,
                        receipt.run_id,
                        review["confirmation_id"],
                        {"action": "clarify", "answer": case["clarification_answer"]},
                        ResumeKey(idempotency_key=uuid4(), token=review["token"]),
                    )
                elif row.state not in ("QUEUED", "RUNNING"):
                    break
                if time.monotonic() - started > 720:
                    failure = "evaluation_deadline"
                    await service.cancel(actor, receipt.run_id, row.task_revision)
                    row = await service.get(actor, receipt.run_id)
                    break
                await asyncio.sleep(0.1 if worker else 0.3)
            elapsed = round(time.monotonic() - started, 3)
            evidence, summary = [], None
            if row.state == "SUCCEEDED":
                published = await HistoricalReports(state).report(actor, receipt.run_id)
                summary = published.summary
                for item in published.evidence:
                    full = await HistoricalReports(state).evidence(
                        actor, receipt.run_id, item["evidence_id"]
                    )
                    evidence.append({**item, "sql": full["sql"], "params": full["params"]})
            async with state.connection() as conn:
                calls = await (
                    await conn.execute(
                        "SELECT kind,reservation,model_name,provider_model,status,usage_status,"
                        "input_tokens,output_tokens,total_tokens,elapsed_ms,error_code "
                        "FROM app_state.model_calls WHERE tenant_id=%s AND run_id=%s "
                        "ORDER BY reservation",
                        (actor.tenant_id, receipt.run_id),
                    )
                ).fetchall()
            matched = any(same_result(expected, item) for item in evidence)
            metrics = (
                set().union(*(set(step["metric_ids"]) for step in row.public_plan))
                if row.public_plan
                else set()
            )
            metric_match = set(case["metric_ids"]).issubset(metrics)
            failure = failure or row.error_code or ("evidence_mismatch" if not matched else None)
            report["cases"].append(
                {
                    "id": case["id"],
                    "question": case["question"],
                    "run_id": str(receipt.run_id),
                    "terminal_state": row.state,
                    "failure_reason": failure,
                    "elapsed_seconds": elapsed,
                    "requires_clarification": case["requires_clarification"],
                    "clarification_count": len(reviews),
                    "clarifications": reviews,
                    "query_used": row.query_used,
                    "model_used": row.model_used,
                    "repair_used": row.repair_used,
                    "evidence_match": matched,
                    "metric_match": metric_match,
                    "expected": expected,
                    "evidence": evidence,
                    "evidence_sha256": [item["sha256"] for item in evidence],
                    "report_summary": summary,
                    "model_calls": calls,
                }
            )
            report["summary"] = summarize(report["cases"])
            report["provider_models"] = sorted(
                {
                    call["provider_model"]
                    for c in report["cases"]
                    for call in c["model_calls"]
                    if call["provider_model"]
                }
            )
            report["updated_at"] = datetime.now(UTC).isoformat()
            save_report(report, report_path)
            print(
                json.dumps(
                    {
                        "case": case["id"],
                        "state": row.state,
                        "evidence_match": matched,
                        "reason": failure,
                        "seconds": elapsed,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return report
