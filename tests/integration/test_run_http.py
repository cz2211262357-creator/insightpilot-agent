from uuid import uuid4

from fastapi.testclient import TestClient

from insightpilot.api import create_app
from insightpilot.config import Settings
from insightpilot.runtime import new_event_loop
from tests.support import PASSWORD, prepare_users


def test_owned_http_conversation_topics_retries_cancel_and_csrf(
    real_dependencies, monkeypatch, workspace_root
):
    dep = real_dependencies
    _, actors = prepare_users(dep)
    dep.configure(monkeypatch)
    with TestClient(
        create_app(Settings.from_environment(workspace_root)),
        backend_options={"loop_factory": new_event_loop},
        headers={"origin": "http://localhost:8090"},
    ) as http:
        login = http.post(
            "/api/v1/auth/login",
            json={
                "tenant_id": str(actors["A_admin"].tenant_id),
                "email": "admin@a.synthetic.invalid",
                "password": PASSWORD,
            },
        )
        assert login.status_code == 200
        headers = {"x-csrf-token": http.cookies.get("ip_csrf")}
        created = http.post("/api/v1/conversations", json={"title": "销售分析"}, headers=headers)
        assert created.status_code == 201, created.text
        initial = created.json()
        conversation = initial["conversation_id"]
        topic = initial["active_topic_id"]
        body = {
            "topic_id": topic,
            "client_message_id": str(uuid4()),
            "question": "八月销售额",
            "scope": "BACKGROUND",
        }
        route = "/api/v1/conversations/" + conversation + "/messages"
        assert http.post(route, json=body).status_code == 403
        response = http.post(route, json=body, headers=headers)
        assert response.status_code == 202, response.text
        receipt = response.json()
        run_id = receipt["run_id"]
        assert http.post(route, json=body, headers=headers).json() == receipt
        switched = http.post(
            "/api/v1/conversations/" + conversation + "/topics",
            json={"title": "第二个话题", "expected_epoch": 1},
            headers=headers,
        )
        assert switched.status_code == 201 and switched.json()["epoch"] == 2
        assert http.get("/api/v1/runs/" + run_id).json()["state"] == "QUEUED"
        stopped = http.post(
            "/api/v1/runs/" + run_id + "/cancel", json={"expected_revision": 1}, headers=headers
        )
        assert stopped.status_code == 200 and stopped.json()["state"] == "CANCELLED"
        assert (
            http.post(
                "/api/v1/runs/" + run_id + "/cancel", json={"expected_revision": 1}, headers=headers
            ).json()
            == stopped.json()
        )
        history = http.get("/api/v1/topics/" + topic + "/history").json()
        assert len(history["runs"]) == 1 and history["runs"][0]["state"] == "CANCELLED"
        assert http.get("/api/v1/conversations/" + conversation).json()["epoch"] == 2
        forged = dict(
            body, client_message_id=str(uuid4()), tenant_id=str(actors["B_admin"].tenant_id)
        )
        assert http.post(route, json=forged, headers=headers).status_code == 422
