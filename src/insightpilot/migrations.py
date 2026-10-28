"Explicit owner-only maintenance; runtime roles cannot run migrations."

import importlib.util
import json
from pathlib import Path

from psycopg import sql

from insightpilot.catalog.models import DatasetVersion

PROJECT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).parent / "resources"
if not ASSETS.is_dir():
    ASSETS = PROJECT


def migrate(conn):
    # Only this project's explicitly named local databases; never a ServiceHub DB.
    database = conn.execute("SELECT current_database() AS name").fetchone()
    name = database["name"] if isinstance(database, dict) else database[0]
    if name not in ("insightpilot", "insightpilot_fixture"):
        raise ValueError("migration_database_scope")
    for file in sorted((ASSETS / "migrations").glob("*.sql")):
        with conn.transaction():
            conn.execute(file.read_text(encoding="utf-8"), prepare=False)


def configure_role_passwords(conn, passwords):
    if set(passwords) != {"ip_state", "ip_checkpoint", "ip_reader"}:
        raise ValueError("invalid_role_set")
    for role, password in passwords.items():
        if len(password) < 24:
            raise ValueError("weak_generated_password")
        conn.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
        )


def load_manifest():
    return json.loads((ASSETS / "fixtures/metrics.json").read_text(encoding="utf-8"))


def seed_synthetic(conn, seed=20261025):
    module_spec = importlib.util.spec_from_file_location(
        "insightpilot_synthetic", ASSETS / "fixtures/generate.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    version, tenants, digest, data = module.build_synthetic(seed)
    with conn.transaction():
        for tenant, label, records in data:
            conn.execute(
                ("INSERT INTO app_state.tenants(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING"),
                (tenant, f"虚构演示商户{label}"),
            )
            previous = conn.execute(
                ("SELECT sha256 FROM app_state.dataset_versions WHERE tenant_id=%s AND id=%s"),
                (tenant, version),
            ).fetchone()
            if previous:
                assert (previous["sha256"] if isinstance(previous, dict) else previous[0]) == digest
                continue
            conn.execute(
                (
                    "INSERT INTO "
                    "app_state.dataset_versions(tenant_id,id,seed,sha256,catalog_versi"
                    "on,as_of) "
                    "VALUES(%s,%s,%s,%s,%s,%s)"
                ),
                (tenant, version, seed, digest, "2026-10-v1", "2026-10-07T00:00:00Z"),
            )
            for table, rows in records.items():
                for row in rows:
                    values = dict(tenant_id=tenant, dataset_version=version, **row)
                    conn.execute(
                        sql.SQL("INSERT INTO business_private.{} ({}) VALUES ({})").format(
                            sql.Identifier(table),
                            sql.SQL(",").join(map(sql.Identifier, values)),
                            sql.SQL(",").join(sql.Placeholder() for _ in values),
                        ),
                        list(values.values()),
                    )
    for table in ("regions", "channels", "products", "orders", "order_items", "aftersales"):
        conn.execute(sql.SQL("ANALYZE business_private.{}").format(sql.Identifier(table)))
    return DatasetVersion(version, tenants, digest)
