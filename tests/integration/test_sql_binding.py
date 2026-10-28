import json
import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from insightpilot.catalog.models import Catalog
from insightpilot.migrations import load_manifest
from insightpilot.query.parameters import compile_bound
from insightpilot.query.policy import SqlPolicy
from tests.support import dsn, prepare_users


def test_actual_bound_percent_modulo_and_injection_literal(real_dependencies):
    dep = real_dependencies
    version, actors = prepare_users(dep)
    catalog = Catalog(dataset_version=version.id, **load_manifest())
    payload = "paid'; DELETE FROM orders;--"
    query = SqlPolicy().validate(
        "SELECT ':status 20%' AS label, :status AS bound, order_id % :mod "
        "AS remainder FROM analysis.orders WHERE customer_ref LIKE "
        ":pattern ORDER BY order_id LIMIT 1",
        {"status": payload, "mod": 2, "pattern": "SYN-A-%"},
        catalog,
    )
    sql, params = compile_bound(query)
    with (
        psycopg.connect(dsn(dep, "ip_reader"), autocommit=True, row_factory=dict_row) as conn,
        conn.transaction(),
    ):
        conn.execute(
            "SELECT set_config('insightpilot.tenant_id',%s,true),set_config('i"
            "nsightpilot.dataset_version',%s,true)",
            (str(actors["A_admin"].tenant_id), str(version.id)),
        )
        row = conn.execute(sql, params).fetchone()
    assert row == {"label": ":status 20%", "bound": payload, "remainder": 1}


def test_twenty_compiled_queries_match_handwritten_gold_for_both_tenants(real_dependencies):
    dep = real_dependencies
    version, actors = prepare_users(dep)
    catalog = Catalog(dataset_version=version.id, **load_manifest())
    cases = json.loads(
        (Path(__file__).resolve().parents[2] / "evals/gold_cases.json").read_text(encoding="utf-8")
    )
    with psycopg.connect(dsn(dep, "ip_reader"), autocommit=True, row_factory=dict_row) as conn:
        for actor in (actors["A_admin"], actors["B_admin"]):
            for case in cases:
                query = SqlPolicy().validate(
                    re.sub(r"%\((\w+)\)s", r":\1", case["sql"]), case["params"], catalog
                )
                bound, params = compile_bound(query)
                with conn.transaction():
                    conn.execute(
                        "SELECT set_config('insightpilot.tenant_id',%s,true),set_config('i"
                        "nsightpilot.dataset_version',%s,true)",
                        (str(actor.tenant_id), str(version.id)),
                    )
                    actual = conn.execute(bound, params).fetchall()
                    expected = conn.execute(case["sql"], case["params"]).fetchall()
                    assert [list(row.values()) for row in actual] == [
                        list(row.values()) for row in expected
                    ], case["id"]
