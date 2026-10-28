"Private database backups; restore verification only targets newly owned containers."

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from scripts.private_files import create_private


def docker_binary():
    root = Path(os.environ["INSIGHTPILOT_WORKSPACE_ROOT"])
    local = root / ".tools/docker/resources/bin/docker.exe"
    result = str(local) if local.is_file() else shutil.which("docker")
    if not result:
        raise RuntimeError("docker_cli_unavailable")
    return result


def backup_container(container, database, directory):
    container.reload()
    if not (
        container.labels.get("io.insightpilot.fixture")
        or (
            container.labels.get("com.docker.compose.project") == "insightpilot"
            and container.labels.get("com.docker.compose.service") == "postgres"
        )
    ):
        raise ValueError("backup_container_scope")
    if database not in ("insightpilot", "insightpilot_fixture"):
        raise ValueError("backup_database_scope")
    result = container.exec_run(["pg_dump", "-U", "postgres", "-Fc", database], user="postgres")
    if result.exit_code != 0 or not result.output.startswith(b"PGDMP"):
        raise RuntimeError("backup_failed")
    directory = Path(directory).resolve()
    root = Path(os.environ["INSIGHTPILOT_WORKSPACE_ROOT"]).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("backup_directory_scope")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("insightpilot-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".dump")
    create_private(path, result.output)
    create_private(
        path.with_suffix(".sha256"),
        (hashlib.sha256(result.output).hexdigest() + "\n").encode("ascii"),
    )
    return path


def verify_restore(path):
    from tests.dependencies import OwnedDependencies
    from tests.support import dsn

    path = Path(path).resolve()
    root = Path(os.environ["INSIGHTPILOT_WORKSPACE_ROOT"]).resolve()
    if not path.is_relative_to(root) or path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("restore_input_scope")
    blob = path.read_bytes()
    if (
        hashlib.sha256(blob).hexdigest()
        != path.with_suffix(".sha256").read_text(encoding="ascii").strip()
    ):
        raise ValueError("backup_checksum_mismatch")
    dep = OwnedDependencies()
    try:
        dep.start()
        target = dep.postgres.get_wrapped_container()
        target.reload()
        assert target.labels["io.insightpilot.fixture"] == dep.identity
        with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
            conn.execute("CREATE ROLE ip_owner NOLOGIN NOSUPERUSER NOBYPASSRLS")
        restored = subprocess.run(
            [
                docker_binary(),
                "exec",
                "-i",
                target.id,
                "pg_restore",
                "-U",
                "postgres",
                "-d",
                "insightpilot_fixture",
                "--exit-on-error",
            ],
            input=blob,
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if restored.returncode:
            raise RuntimeError("restore_verification_failed")
        with psycopg.connect(dsn(dep, "postgres"), autocommit=True, row_factory=dict_row) as conn:
            versions = conn.execute(
                "SELECT tenant_id,id,sha256 FROM app_state.dataset_versions ORDER BY tenant_id"
            ).fetchall()
            orders = conn.execute("SELECT count(*) AS n FROM business_private.orders").fetchone()[
                "n"
            ]
            schema = conn.execute(
                "SELECT version FROM app_state.schema_version WHERE singleton"
            ).fetchone()["version"]
            roles = conn.execute(
                "SELECT count(*) AS n FROM pg_roles WHERE rolname IN "
                "('ip_state','ip_checkpoint','ip_reader') AND NOT rolsuper AND "
                "NOT rolbypassrls"
            ).fetchone()["n"]
        scoped = []
        with psycopg.connect(dsn(dep, "ip_reader"), autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM analysis.orders").fetchone()[0] == 0
            for version in versions:
                with conn.transaction():
                    conn.execute(
                        "SELECT set_config('insightpilot.tenant_id',%s,true),set_config('i"
                        "nsightpilot.dataset_version',%s,true)",
                        (str(version["tenant_id"]), str(version["id"])),
                    )
                    scoped.append(
                        conn.execute("SELECT count(*) FROM analysis.orders").fetchone()[0]
                    )
            assert conn.execute("SELECT count(*) FROM analysis.orders").fetchone()[0] == 0
        return {
            "verified": True,
            "backup_sha256": hashlib.sha256(blob).hexdigest(),
            "orders": orders,
            "dataset_sha256": sorted({v["sha256"] for v in versions}),
            "schema_version": schema,
            "distinct_runtime_roles": roles,
            "rls_counts": sorted(scoped),
            "unscoped_rows": 0,
        }
    finally:
        dep.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    if args.action == "verify":
        if args.path is None:
            parser.error("--path is required")
        print(json.dumps(verify_restore(args.path)))
    else:
        import docker

        with closing(docker.from_env()) as client:
            containers = client.containers.list(
                filters={
                    "label": [
                        "com.docker.compose.project=insightpilot",
                        "com.docker.compose.service=postgres",
                    ]
                }
            )
            if len(containers) != 1:
                raise RuntimeError("project_postgres_not_unique")
            root = Path(os.environ["INSIGHTPILOT_WORKSPACE_ROOT"])
            path = backup_container(
                containers[0], "insightpilot", root / ".data/insightpilot/backups"
            )
            print(json.dumps({"backup_path": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
