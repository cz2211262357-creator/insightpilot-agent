import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from insightpilot.catalog.models import Catalog
from insightpilot.errors import DomainError
from insightpilot.migrations import load_manifest
from insightpilot.query.parameters import compile_bound
from insightpilot.query.policy import SqlPolicy


@pytest.fixture
def catalog():
    return Catalog(dataset_version=uuid4(), **load_manifest())


VALID = [
    "SELECT order_id,paid_amount FROM analysis.orders WHERE status=:status",
    "SELECT COUNT(*) AS total FROM analysis.orders",
    "SELECT * FROM analysis.orders",
    "WITH valid AS (SELECT order_id,paid_amount FROM analysis.orders) "
    "SELECT SUM(v.paid_amount) FROM valid v",
    "SELECT o.order_id FROM analysis.orders o WHERE EXISTS(SELECT 1 "
    "FROM analysis.aftersales a WHERE a.order_id=o.order_id)",
    "SELECT CASE WHEN paid_amount>0 THEN 'paid' ELSE 'zero' END AS label FROM analysis.orders",
    "SELECT '; DROP TABLE orders' AS safe_label FROM analysis.orders LIMIT 1",
]


@pytest.mark.parametrize("sql", VALID)
def test_safe_selects_ctes_aliases_and_star_are_qualified(sql, catalog):
    params = {"status": "paid"} if ":status" in sql else {}
    query = SqlPolicy().validate(sql, params, catalog)
    assert query.views == ["orders"] or set(query.views) == {"orders", "aftersales"}
    assert len(query.sql_hash) == 64
    assert "analysis.orders" in query.sql
    if "SELECT * FROM" in sql:
        assert "SELECT *" not in query.sql


DENIED = [
    "SELECT 1; DELETE FROM analysis.orders",
    "WITH x AS(DELETE FROM analysis.orders RETURNING *) SELECT * FROM x",
    "WITH x AS(UPDATE analysis.orders SET paid_amount=0 RETURNING *) SELECT * FROM x",
    "SELECT * INTO public.stolen FROM analysis.orders",
    "SELECT * FROM analysis.orders FOR UPDATE",
    "COPY analysis.orders TO STDOUT",
    "SELECT pg_sleep(1)",
    "SELECT set_config('insightpilot.tenant_id','x',true)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_ls_dir('/')",
    "SELECT public.untrusted_udf(order_id) FROM analysis.orders",
    "SELECT * FROM pg_catalog.pg_authid",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM business_private.orders",
    "SELECT * FROM app_state.users",
    "SELECT * FROM graph_checkpoint.checkpoints",
    "SELECT * FROM otherdb.analysis.orders",
    "SELECT password_hash FROM analysis.orders",
    "SELECT o.password_hash FROM analysis.orders o",
    "WITH x AS(SELECT password_hash FROM analysis.orders) SELECT * FROM x",
    "SELECT order_id FROM analysis.orders o JOIN analysis.order_items i ON i.order_id=o.order_id",
    'SELECT "Order_Id" FROM analysis.orders',
    'SELECT * FROM analysis."Orders"',
    "WITH RECURSIVE x AS(SELECT order_id FROM analysis.orders UNION "
    "ALL SELECT order_id FROM x) SELECT * FROM x",
    "SELECT * FROM generate_series(1,1000000)",
    "SELECT 'pg_authid'::regclass",
    "SELECT '{1,2}'::int[]",
    "SELECT current_user",
    "SELECT * FROM analysis.orders LIMIT 1001",
    "SELECT * FROM analysis.orders LIMIT (SELECT 1)",
    "SELECT * FROM analysis.orders WHERE order_id=$1",
    "SELECT * FROM analysis.orders UNION SELECT * FROM app_state.users",
]


@pytest.mark.parametrize("sql", DENIED)
def test_denies_recursive_dml_functions_relations_columns_and_limits(sql, catalog):
    with pytest.raises(DomainError) as error:
        SqlPolicy().validate(sql, {}, catalog)
    assert error.value.code.startswith("sql_") and error.value.status == 422


@pytest.mark.parametrize(
    "sql,params",
    [
        ("SELECT :name", {}),
        ("SELECT :name", {"name": "x", "unused": "y"}),
        ("SELECT :name", {"name": 1.25}),
        ("SELECT :name", {"name": ["nested"]}),
        ("SELECT :name", {"name": "x" * 2049}),
        ("SELECT :name", {"name": 2**70}),
        ("SELECT :name", {"name": None, **{f"p{i}": 0 for i in range(32)}}),
    ],
)
def test_parameters_are_exact_bounded_scalar_bindings(sql, params, catalog):
    with pytest.raises(DomainError) as error:
        SqlPolicy().validate(sql, params, catalog)
    assert error.value.code == "sql_parameters_invalid"


def test_parameter_generator_preserves_literal_colons_and_percent(catalog):
    payload = "paid'; DROP TABLE orders;--"
    query = SqlPolicy().validate(
        "SELECT ':status 20%' AS label, :status AS bound FROM "
        "analysis.orders WHERE customer_ref LIKE 'SYN-%' LIMIT 1",
        {"status": payload},
        catalog,
    )
    compiled, params = compile_bound(query)
    assert "':status 20%%'" in compiled and "'SYN-%%'" in compiled
    assert "%(status)s" in compiled and payload not in compiled
    assert params == {"status": payload}
    same = SqlPolicy().validate(query.sql, {"status": payload}, catalog)
    assert same.sql_hash == query.sql_hash


def test_all_twenty_gold_queries_follow_same_policy(catalog):
    cases = json.loads(
        (Path(__file__).resolve().parents[2] / "evals/gold_cases.json").read_text(encoding="utf-8")
    )
    for case in cases:
        sql = re.sub(r"%\((\w+)\)s", r":\1", case["sql"])
        result = SqlPolicy().validate(sql, case["params"], catalog)
        assert len(result.sql_hash) == 64 and result.policy_version == "2026-10-v1"


def test_limits_length_and_nested_subqueries(catalog):
    with pytest.raises(DomainError):
        SqlPolicy().validate("SELECT 1 " + " " * 16000, {}, catalog)
    sql = "SELECT order_id FROM analysis.orders"
    for i in range(10):
        sql = f"SELECT * FROM ({sql}) q{i}"
    with pytest.raises(DomainError):
        SqlPolicy().validate(sql, {}, catalog)
