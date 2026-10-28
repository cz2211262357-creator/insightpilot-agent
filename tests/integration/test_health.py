from fastapi.testclient import TestClient

from insightpilot.api import create_app
from insightpilot.config import Settings
from insightpilot.runtime import new_event_loop


def test_liveness_remains_available_without_database_connections(workspace_root):
    with TestClient(
        create_app(Settings.from_environment(workspace_root)),
        backend_options={"loop_factory": new_event_loop},
    ) as client:
        response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_returns_safe_dependency_error_when_connections_are_unavailable(workspace_root):
    with TestClient(
        create_app(Settings.from_environment(workspace_root)),
        backend_options={"loop_factory": new_event_loop},
    ) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "dependency_unavailable"
    assert len(error["request_id"]) > 0
    for forbidden in [
        "synthetic-secret",
        "postgresql://",
        "redis://",
        "ip_state",
        "synthetic-not-a-provider-key",
    ]:
        assert forbidden not in response.text


def test_readiness_requires_successful_connections_to_real_separate_roles(
    workspace_root, monkeypatch, real_dependencies
):
    import psycopg

    from insightpilot.checkpoints import setup_checkpoints
    from tests.support import dsn, prepare_users, run

    prepare_users(real_dependencies)

    async def setup():
        async with await psycopg.AsyncConnection.connect(
            dsn(real_dependencies, "postgres"), autocommit=True, row_factory=psycopg.rows.dict_row
        ) as conn:
            await setup_checkpoints(conn)

    run(setup())
    real_dependencies.configure(monkeypatch)
    with TestClient(
        create_app(Settings.from_environment(workspace_root)),
        backend_options={"loop_factory": new_event_loop},
    ) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "status": "ready",
        "dependencies": {"state": True, "checkpoint": True, "query": True, "redis": True},
    }


def test_real_dependency_outage_is_reported_and_recovers(
    workspace_root, monkeypatch, real_dependencies
):
    import psycopg

    from insightpilot.checkpoints import setup_checkpoints
    from tests.support import dsn, prepare_users, run

    prepare_users(real_dependencies)

    async def setup():
        async with await psycopg.AsyncConnection.connect(
            dsn(real_dependencies, "postgres"), autocommit=True, row_factory=psycopg.rows.dict_row
        ) as conn:
            await setup_checkpoints(conn)

    run(setup())
    real_dependencies.configure(monkeypatch)
    with TestClient(
        create_app(Settings.from_environment(workspace_root)),
        backend_options={"loop_factory": new_event_loop},
    ) as client:
        assert client.get("/api/v1/health/ready").status_code == 200
        redis = real_dependencies.redis.get_wrapped_container()
        redis.pause()
        try:
            redis.reload()
            assert redis.attrs["State"]["Paused"] is True
            response = client.get("/api/v1/health/ready")
            assert response.status_code == 503
            status = response.json()["error"]["details"]["dependencies"]
            assert status == {"state": True, "checkpoint": True, "query": True, "redis": False}
        finally:
            redis.unpause()
        assert client.get("/api/v1/health/ready").status_code == 200
