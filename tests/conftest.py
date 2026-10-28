import os
import socket
from pathlib import Path

import pytest


@pytest.fixture
def workspace_root():
    return Path(os.environ["INSIGHTPILOT_WORKSPACE_ROOT"])


@pytest.fixture(autouse=True)
def private_test_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("INSIGHTPILOT_") and key != "INSIGHTPILOT_WORKSPACE_ROOT":
            monkeypatch.delenv(key)
    monkeypatch.setenv("INSIGHTPILOT_MODEL_API_KEY", "synthetic-not-a-provider-key")
    monkeypatch.setenv(
        "INSIGHTPILOT_MCP_SIGNING_KEY", "synthetic-test-key-only-at-least-32-characters"
    )
    sockets = [socket.socket(), socket.socket()]
    for listener in sockets:
        listener.bind(("127.0.0.1", 0))
    # Bound without listen: connection refusal on ports owned by this test.
    postgres_port, redis_port = [listener.getsockname()[1] for listener in sockets]
    for name, role in [
        ("STATE", "ip_state"),
        ("CHECKPOINT", "ip_checkpoint"),
        ("QUERY", "ip_reader"),
    ]:
        monkeypatch.setenv(
            f"INSIGHTPILOT_POSTGRES_{name}_DSN",
            f"postgresql://{role}:synthetic-secret@127.0.0.1:{postgres_port}/insightpilot_test",
        )
    monkeypatch.setenv(
        "INSIGHTPILOT_REDIS_URL", f"redis://:synthetic-secret@127.0.0.1:{redis_port}/0"
    )
    try:
        yield
    finally:
        for listener in sockets:
            listener.close()


@pytest.fixture(scope="session")
def real_dependencies():
    from tests.dependencies import OwnedDependencies

    dependencies = OwnedDependencies()
    try:
        dependencies.start()
        yield dependencies
    finally:
        dependencies.close()


@pytest.fixture(autouse=True)
def isolate_owned_application_state(request):
    "No queued jobs/sessions leak between cases; never touches a user database."
    if "real_dependencies" not in request.fixturenames:
        return
    import psycopg
    from redis import Redis

    from tests.support import dsn

    dep = request.getfixturevalue("real_dependencies")
    assert dep.postgres.get_wrapped_container().labels["io.insightpilot.fixture"] == dep.identity
    with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
        assert conn.execute("SELECT current_database()").fetchone()[0] == "insightpilot_fixture"
        if conn.execute("SELECT to_regclass('app_state.conversations')").fetchone()[0]:
            conn.execute(
                "TRUNCATE app_state.conversations,app_state.sessions,"
                "app_state.audits RESTART IDENTITY CASCADE"
            )
    with Redis(
        host="127.0.0.1",
        port=dep.redis_port,
        password=dep.password,
        socket_timeout=1,
        socket_connect_timeout=1,
    ) as client:
        keys = list(client.scan_iter(match="insightpilot:*"))
        if keys:
            client.delete(*keys)
