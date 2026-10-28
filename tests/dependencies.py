"Owned, loopback-only real containers with explicit cleanup; never user databases."

import os
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from testcontainers.core.config import testcontainers_config
from testcontainers.core.container import DockerContainer
from testcontainers.core.exceptions import ContainerStartException

POSTGRES = "postgres:18@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6"
REDIS = "redis:8.4@sha256:43afe4a9a43080e64dfa29699eef903fc9d54991fe8b03d57237c05bed2a396c"


class OwnedDependencies:
    def __init__(self):
        self.identity = uuid4().hex
        self.password = uuid4().hex
        self.owner_password = uuid4().hex
        self.containers = []

    def _container(self, image, port):
        container = DockerContainer(image).with_bind_ports(port)
        # SDK4.15's with_bind_ports accepts only a host port, not an IP.
        # Docker SDK's public ports mapping supports an IP/ephemeral-port tuple.
        container.ports[str(port)] = ("127.0.0.1", None)
        container.with_kwargs(
            labels={
                "io.insightpilot.fixture": self.identity,
                "io.insightpilot.fixture-parent": str(os.getpid()),
            }
        )
        self.containers.append(container)
        return container

    def start(self):
        # The default Python Ryuk port binds all interfaces. Keep every published
        # port on loopback and clean our explicitly recorded handles in finally.
        testcontainers_config.ryuk_disabled = True
        self.postgres = self._container(POSTGRES, 5432)
        self.postgres.with_env("POSTGRES_PASSWORD", self.owner_password)
        self.postgres.with_env("POSTGRES_DB", "insightpilot_fixture")
        self.postgres.start()
        self.port = int(self.postgres.get_exposed_port(5432))
        deadline = time.monotonic() + 60
        while True:
            try:
                connection = psycopg.connect(
                    host="127.0.0.1",
                    port=self.port,
                    user="postgres",
                    password=self.owner_password,
                    dbname="insightpilot_fixture",
                    connect_timeout=1,
                    autocommit=True,
                )
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Owned Postgres did not become ready") from None
                time.sleep(0.1)
        with connection:
            for role in ("ip_state", "ip_checkpoint", "ip_reader"):
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                        sql.Identifier(role), sql.Literal(self.password)
                    )
                )
        self.redis = self._container(REDIS, 6379)
        self.redis.with_command(f"redis-server --requirepass {self.password}")
        self.redis.start()
        self.redis_port = int(self.redis.get_exposed_port(6379))
        from redis import Redis

        client = Redis(
            host="127.0.0.1",
            port=self.redis_port,
            password=self.password,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if client.ping():
                        break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Owned Redis did not become ready") from None
                    time.sleep(0.1)
        finally:
            client.close()
        for container in self.containers:
            container.get_wrapped_container().reload()
            bindings = container.get_wrapped_container().attrs["NetworkSettings"]["Ports"]
            assert all(
                binding["HostIp"] == "127.0.0.1"
                for rows in bindings.values()
                if rows
                for binding in rows
            )
        return self

    def configure(self, monkeypatch):
        for name, role in [
            ("STATE", "ip_state"),
            ("CHECKPOINT", "ip_checkpoint"),
            ("QUERY", "ip_reader"),
        ]:
            monkeypatch.setenv(
                f"INSIGHTPILOT_POSTGRES_{name}_DSN",
                f"postgresql://{role}:{self.password}@127.0.0.1:{self.port}/insightpilot_fixture",
            )
        monkeypatch.setenv(
            "INSIGHTPILOT_REDIS_URL", f"redis://:{self.password}@127.0.0.1:{self.redis_port}/0"
        )

    def close(self):
        for container in reversed(self.containers):
            try:
                owned = container.get_wrapped_container()
            except ContainerStartException:
                continue
            owned.reload()
            assert owned.labels.get("io.insightpilot.fixture") == self.identity
            container.stop()
