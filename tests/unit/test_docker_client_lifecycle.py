from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import docker

from scripts import backup, online_check


class PlainDockerClient:
    """Matches the SDK: close() exists; __enter__/__exit__ do not."""

    def __init__(self, containers):
        self.containers = containers
        self.close = Mock()


def test_backup_cli_closes_docker_sdk_client(monkeypatch, tmp_path):
    client = PlainDockerClient(SimpleNamespace(list=Mock(return_value=[object()])))
    monkeypatch.setattr(docker, "from_env", lambda: client)
    monkeypatch.setattr(backup, "backup_container", lambda *args: tmp_path / "synthetic.dump")
    monkeypatch.setenv("INSIGHTPILOT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["backup", "create"])
    backup.main()
    client.close.assert_called_once()


def test_recovery_check_closes_docker_sdk_client(monkeypatch):
    run_id = str(uuid4())
    worker = SimpleNamespace(
        labels={"com.docker.compose.project": "insightpilot"},
        reload=Mock(),
        kill=Mock(),
        start=Mock(),
    )
    client = PlainDockerClient(SimpleNamespace(list=Mock(return_value=[worker])))
    monkeypatch.setattr(docker, "from_env", lambda: client)

    class Cursor:
        def fetchall(self):
            return [(run_id,)]

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args):
            return Cursor()

    monkeypatch.setattr(online_check.psycopg, "connect", lambda *args, **kwargs: Conn())
    runner = online_check.Acceptance.__new__(online_check.Acceptance)
    runner.settings = SimpleNamespace(
        postgres_state_dsn=SimpleNamespace(get_secret_value=lambda: "synthetic-dsn")
    )
    runner.submit = Mock(return_value={"run_id": run_id})
    runner.wait = Mock(
        side_effect=[
            {"state": "RUNNING"},
            {"state": "SUCCEEDED", "execution_version": 2, "attempts": 2},
        ]
    )
    runner.record = Mock()
    runner.recover()
    worker.kill.assert_called_once_with(signal="SIGKILL")
    worker.start.assert_called_once()
    client.close.assert_called_once()


def test_acceptance_cleanup_cancels_only_its_own_unfinished_run():
    run_id = str(uuid4())
    runner = online_check.Acceptance.__new__(online_check.Acceptance)
    runner.owned_runs = {run_id}
    runner.snapshot = Mock(return_value={"state": "WAITING_INPUT", "task_revision": 3})
    runner.request = Mock(return_value={})
    runner.http = SimpleNamespace(close=Mock())
    runner.close()
    assert runner.request.call_args_list[0].args == (
        "POST",
        "/runs/" + run_id + "/cancel",
        {"expected_revision": 3},
    )
    assert runner.request.call_args_list[1].args == ("POST", "/auth/logout")
    runner.http.close.assert_called_once()


def test_costly_check_keeps_planning_clarification_separate_from_approval():
    run_id = str(uuid4())
    runner = online_check.Acceptance.__new__(online_check.Acceptance)
    runner.submit = Mock(return_value={"run_id": run_id})
    runner.wait = Mock(
        side_effect=[
            {"state": "WAITING_INPUT"},
            {"state": "WAITING_INPUT"},
            {"state": "SUCCEEDED", "query_used": 1},
        ]
    )
    runner.request = Mock(
        side_effect=[
            {"kind": "clarification"},
            {"kind": "query_approval", "explain": {"approval_reasons": ["high_cost"]}},
            {"evidence": [{"rows": [{"order_count": 96**3}]}]},
        ]
    )
    runner.resume = Mock()
    runner.snapshot = Mock(return_value={"model_used": 2})
    runner.record = Mock()
    runner.costly()
    planning = runner.resume.call_args_list[0].args[2]
    approval = runner.resume.call_args_list[1].args[2]
    assert planning["action"] == "clarify"
    assert "执行前" in planning["answer"] and "审批" in planning["answer"]
    assert approval == {"action": "approve"}
