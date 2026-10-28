import asyncio
import json
from pathlib import Path
from uuid import NAMESPACE_DNS, uuid5

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from insightpilot.catalog.repository import CatalogRepository
from insightpilot.migrations import migrate, seed_synthetic
from insightpilot.runtime import new_event_loop
from insightpilot.types import Actor, Role

TENANTS = tuple(uuid5(NAMESPACE_DNS, f"insightpilot.synthetic.20261025.{x}") for x in ("A", "B"))
P = Path(__file__).resolve().parents[2]


@pytest.fixture
def data(real_dependencies):
    dep = real_dependencies
    admin = (
        f"host=127.0.0.1 port={dep.port} user=postgres "
        f"password={dep.owner_password} dbname=insightpilot_fixture"
    )
    with psycopg.connect(admin, autocommit=True, row_factory=dict_row) as conn:
        migrate(conn)
        version = seed_synthetic(conn)
    return dep, version, admin


def dsn(dep, role):
    return (
        f"host=127.0.0.1 port={dep.port} user={role} "
        f"password={dep.password} dbname=insightpilot_fixture"
    )


def context(conn, tenant, version):
    conn.execute(
        (
            "SELECT set_config('insightpilot.tenant_id', %s, true), "
            "set_config('insightpilot.dataset_version', %s, true)"
        ),
        (str(tenant), str(version)),
    )


def test_actual_views_pool_rls_and_no_context(data):
    dep, version, _ = data
    assert version is not None, "migration must seed a real version"
    results = []
    with ConnectionPool(
        dsn(dep, "ip_reader"),
        min_size=1,
        max_size=1,
        kwargs={"autocommit": True, "row_factory": dict_row},
    ) as pool:
        for tenant in TENANTS:
            with pool.connection() as conn, conn.transaction():
                context(conn, tenant, version.id)
                rows = conn.execute(
                    "SELECT customer_ref, paid_amount FROM analysis.orders ORDER BY order_id"
                ).fetchall()
                assert rows
                assert all(
                    row["customer_ref"].startswith("SYN-" + ("A" if tenant == TENANTS[0] else "B"))
                    for row in rows
                )
                results.append(rows)
        with pool.connection() as conn:
            assert conn.execute("SELECT * FROM analysis.orders").fetchall() == []
            assert (
                conn.execute(
                    "SELECT nullif(current_setting('insightpilot.tenant_id', true), '') AS tenant"
                ).fetchone()["tenant"]
                is None
            )
    assert results[0] != results[1]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM business_private.orders",
        "SELECT * FROM app_state.users",
        "SELECT * FROM graph_checkpoint.checkpoints",
        "SELECT * FROM pg_catalog.pg_authid",
        "UPDATE analysis.orders SET paid_amount = 0",
        "CREATE TABLE public.stolen(id int)",
        "CREATE TEMP TABLE stolen(id int)",
    ],
)
def test_reader_database_boundaries(data, statement):
    dep, _, _ = data
    with psycopg.connect(dsn(dep, "ip_reader"), autocommit=True) as conn:
        # Prove ACL denial independently of the extra default readonly layer.
        conn.execute("SET default_transaction_read_only=off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


def test_invoker_force_rls_and_role_properties(data):
    dep, version, admin = data
    assert version is not None
    with psycopg.connect(admin, row_factory=dict_row) as conn:
        role = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='ip_reader'"
        ).fetchone()
        assert role == {"rolsuper": False, "rolbypassrls": False}
        tables = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity, "
            "pg_get_userbyid(relowner) AS "
            "owner FROM pg_class WHERE relnamespace='business_private'::regnamespace "
            "AND relkind='r'"
        ).fetchall()
        assert len(tables) == 6
        assert all(
            t["relrowsecurity"] and t["relforcerowsecurity"] and t["owner"] != "ip_reader"
            for t in tables
        )
        views = conn.execute(
            "SELECT reloptions FROM pg_class "
            "WHERE relnamespace='analysis'::regnamespace AND relkind='v'"
        ).fetchall()
        assert len(views) == 6
        assert all("security_invoker=true" in v["reloptions"] for v in views)
        assert conn.execute(
            "SELECT has_schema_privilege('ip_reader','business_private','USAGE') AS allowed"
        ).fetchone() == {"allowed": False}
        assert conn.execute(
            "SELECT has_table_privilege('ip_reader','business_private.orders','SELECT') AS allowed"
        ).fetchone() == {"allowed": True}


def test_repeated_migration_seed_and_all_gold_queries(data):
    dep, version, admin = data
    assert version is not None
    with psycopg.connect(admin, autocommit=True, row_factory=dict_row) as conn:
        migrate(conn)
        repeated = seed_synthetic(conn)
    assert repeated == version
    cases = json.loads((P / "evals/gold_cases.json").read_text(encoding="utf-8"))
    assert len(cases) == 20 and len({c["id"] for c in cases}) == 20
    with psycopg.connect(dsn(dep, "ip_reader"), autocommit=True, row_factory=dict_row) as conn:
        for tenant in TENANTS:
            for case in cases:
                with conn.transaction():
                    conn.execute("SET TRANSACTION READ ONLY")
                    context(conn, tenant, version.id)
                    result = conn.execute(case["sql"], case["params"]).fetchall()
                    assert result is not None


def test_catalog_is_scoped_and_metric_404(data):
    dep, version, _ = data
    assert version is not None

    async def check():
        async with AsyncConnectionPool(
            dsn(dep, "ip_state"), kwargs={"autocommit": True, "row_factory": dict_row}, open=False
        ) as pool:
            repo = CatalogRepository(pool)
            actor = Actor(
                TENANTS[0], uuid5(NAMESPACE_DNS, "insightpilot.synthetic.admin.A"), Role.ADMIN, 1
            )
            catalog = await repo.list_datasets(actor)
            assert len(catalog.views) == 6 and len(catalog.metrics) == 6
            assert catalog.dataset_version == version.id
            definition = await repo.get_metric_definition(actor, "paid_sales")
            assert definition.grain == "order" and definition.timezone == "Asia/Shanghai"
            with pytest.raises(LookupError, match="metric_not_found"):
                await repo.get_metric_definition(actor, "arbitrary_profit")

    with asyncio.Runner(loop_factory=new_event_loop) as runner:
        runner.run(check())


def test_item_sums_do_not_fan_out_and_cross_tenant_fk_rejected(data):
    dep, version, admin = data
    with psycopg.connect(admin, autocommit=True, row_factory=dict_row) as conn:
        invalid = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT "
            "o.tenant_id,o.order_id,o.paid_amount,SUM(i.line_paid_amount) AS actual "
            "FROM business_private.orders o JOIN business_private.order_items i "
            "USING(tenant_id,dataset_version,order_id) GROUP BY "
            "o.tenant_id,o.order_id,o.paid_amount HAVING "
            "SUM(i.line_paid_amount)<>o.paid_amount) s"
        ).fetchone()
        assert invalid["n"] == 0
        user_id = uuid5(NAMESPACE_DNS, "rls-fk-fixture-user")
        with conn.transaction():
            conn.execute(
                (
                    "INSERT INTO "
                    "app_state.users(id,tenant_id,email,display_name,password_hash,role) "
                    "VALUES(%s,%s,'fk@synthetic.invalid','FK "
                    "fixture','unusable','analyst') ON "
                    "CONFLICT DO NOTHING"
                ),
                (user_id, TENANTS[0]),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                conn.execute(
                    ("INSERT INTO app_state.conversations(id,tenant_id,user_id) VALUES(%s,%s,%s)"),
                    (uuid5(NAMESPACE_DNS, "cross-tenant-conversation"), TENANTS[1], user_id),
                )
        acl = conn.execute(
            "SELECT has_database_privilege('ip_reader',current_database(),'TEMP') AS "
            "temp,has_database_privilege('ip_reader',current_database(),'CREATE') AS "
            "create"
        ).fetchone()
        assert acl == {"temp": False, "create": False}
