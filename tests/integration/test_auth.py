import hashlib

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from insightpilot.api import create_app
from insightpilot.auth.service import require_current
from insightpilot.config import Settings
from insightpilot.errors import DomainError
from insightpilot.runtime import new_event_loop
from tests.support import PASSWORD, dsn, prepare_users, run

ORIGIN = "http://localhost:8090"


@pytest.fixture
def identities(real_dependencies):
    return real_dependencies, prepare_users(real_dependencies)[1]


@pytest.fixture
def client(identities, monkeypatch, workspace_root):
    dep, actors = identities
    dep.configure(monkeypatch)
    app = create_app(Settings.from_environment(workspace_root))
    with TestClient(
        app, backend_options={"loop_factory": new_event_loop}, headers={"origin": ORIGIN}
    ) as client:
        yield client, actors


def login(client, actor, email="admin@a.synthetic.invalid"):
    response = client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(actor.tenant_id), "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response


def csrf(client):
    return {"x-csrf-token": client.cookies.get("ip_csrf")}


def test_login_cookie_digest_and_logout(client, identities):
    http, actors = client
    response = login(http, actors["A_admin"])
    cookie = http.cookies.get("ip_session")
    assert len(cookie) >= 43
    assert "HttpOnly" in response.headers["set-cookie"]
    assert http.get("/api/v1/auth/me").json()["user_id"] == str(actors["A_admin"].user_id)
    dep, _ = identities
    with psycopg.connect(dsn(dep, "postgres"), row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT token_hash,csrf_hash FROM app_state.sessions WHERE user_id=%s",
            (actors["A_admin"].user_id,),
        ).fetchone()
        assert row["token_hash"] == hashlib.sha256(cookie.encode()).hexdigest()
        assert cookie not in str(row) and http.cookies.get("ip_csrf") not in str(row)
    assert http.post("/api/v1/auth/logout", headers=csrf(http)).status_code == 204
    assert http.get("/api/v1/auth/me").status_code == 401


def test_origin_csrf_and_validation_do_not_echo_secret(client):
    http, actors = client
    response = http.post(
        "/api/v1/auth/login",
        headers={"origin": "https://attacker.invalid"},
        json={
            "tenant_id": str(actors["A_admin"].tenant_id),
            "email": "admin@a.synthetic.invalid",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 403
    login(http, actors["A_admin"])
    assert http.post("/api/v1/auth/logout").status_code == 403
    assert http.post("/api/v1/auth/logout", headers={"x-csrf-token": "forged"}).status_code == 403
    invalid = http.post(
        "/api/v1/auth/login", json={"tenant_id": "bad-uuid", "password": "never-echo-this-secret"}
    )
    assert invalid.status_code == 422 and "never-echo" not in invalid.text


@pytest.mark.parametrize(
    "change", ["enabled=false", "role='analyst'", "auth_version=auth_version+1"]
)
def test_dynamic_revocation_invalidates_existing_cookie(client, identities, change):
    http, actors = client
    login(http, actors["A_admin"])
    dep, _ = identities
    with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as conn:
        # Fixed test variants, never untrusted input.
        conn.execute(
            f"UPDATE app_state.users SET {change} WHERE id=%s", (actors["A_admin"].user_id,)
        )
    assert http.get("/api/v1/auth/me").status_code == 401


def test_members_are_tenant_scoped_and_admin_only(client):
    http, actors = client
    login(http, actors["A_admin"])
    assert http.get("/api/v1/members/" + str(actors["B_admin"].user_id)).status_code == 404
    target = "/api/v1/members/" + str(actors["A_other"].user_id)
    changed = http.patch(target, json={"enabled": False}, headers=csrf(http))
    assert (
        changed.status_code == 200
        and changed.json()["auth_version"] == actors["A_other"].auth_version + 1
    )
    http.post("/api/v1/auth/logout", headers=csrf(http))
    login(http, actors["A_analyst"], "analyst@a.synthetic.invalid")
    assert http.get("/api/v1/members/" + str(actors["A_admin"].user_id)).status_code == 403
    assert http.patch(target, json={"enabled": True}, headers=csrf(http)).status_code == 403


def test_authorization_lock_closes_check_then_write_race(identities):
    dep, actors = identities
    actor = actors["A_admin"]

    async def check():
        async with await psycopg.AsyncConnection.connect(
            dsn(dep), autocommit=True, row_factory=dict_row
        ) as state:
            async with await psycopg.AsyncConnection.connect(
                dsn(dep, "postgres"), autocommit=True, row_factory=dict_row
            ) as admin:
                async with state.transaction():
                    await require_current(state, actor, for_write=True)
                    # Independent revocation transaction must block on the protected identity.
                    with pytest.raises(psycopg.errors.LockNotAvailable):
                        async with admin.transaction():
                            await admin.execute("SET LOCAL lock_timeout='150ms'")
                            await admin.execute(
                                "UPDATE app_state.users SET enabled=false WHERE id=%s",
                                (actor.user_id,),
                            )
                await admin.execute(
                    "UPDATE app_state.users SET enabled=false WHERE id=%s", (actor.user_id,)
                )
                with pytest.raises(DomainError) as denied:
                    async with state.transaction():
                        await require_current(state, actor, for_write=True)
                assert denied.value.status == 401

    run(check())


def test_account_creation_cannot_overwrite_existing_identity(identities):
    from uuid import uuid4

    from insightpilot.auth.service import create_account

    dep, actors = identities

    async def check():
        async with await psycopg.AsyncConnection.connect(
            dsn(dep), autocommit=True, row_factory=dict_row
        ) as conn:
            email = f"new-{uuid4().hex}@synthetic.invalid"
            user_id = await create_account(
                conn,
                actors["A_admin"].tenant_id,
                email,
                "Synthetic new analyst",
                "analyst",
                PASSWORD,
            )
            with pytest.raises(DomainError) as duplicate:
                await create_account(
                    conn, actors["A_admin"].tenant_id, email, "overwritten", "admin", PASSWORD
                )
            assert duplicate.value.code == "account_exists"
            row = await (
                await conn.execute(
                    "SELECT role,display_name FROM app_state.users WHERE id=%s", (user_id,)
                )
            ).fetchone()
            assert row == {"role": "analyst", "display_name": "Synthetic new analyst"}

    run(check())
