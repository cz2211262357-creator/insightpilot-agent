import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import httpx2
import jwt
import psycopg
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from insightpilot.auth.tool_tokens import ToolTokenIssuer
from insightpilot.config import Settings
from insightpilot.mcp_client import DataToolsClient
from insightpilot.runs.models import Scope
from insightpilot.runs.repository import RunRepository
from insightpilot.runs.service import RunService
from tests.integration.test_run_races import scenario as scenario
from tests.support import dsn, make_conversation, run


@pytest.fixture
def mcp_process(scenario, monkeypatch, workspace_root):
    dep, actors, conversation = scenario
    dep.configure(monkeypatch)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    url = f"http://127.0.0.1:{port}/mcp"
    monkeypatch.setenv("INSIGHTPILOT_MCP_URL", url)
    settings = Settings.from_environment(workspace_root)
    log = workspace_root / ".cache/insightpilot-verification" / f"mcp-{uuid4().hex}.log"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "insightpilot.cli",
                "mcp",
                "--workspace-root",
                str(workspace_root),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 20
            with httpx.Client(trust_env=False, timeout=0.3) as http:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Owned MCP child exited; inspect private fixture log")
                    try:
                        if http.get(url.replace("/mcp", "/api/v1/health/live")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("Owned MCP child did not start")
                    time.sleep(0.1)
            yield dep, actors, conversation, settings, url, process.pid
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def state_pool(dep):
    return AsyncConnectionPool(
        dsn(dep),
        open=False,
        min_size=0,
        max_size=6,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )


async def begin(state, actor, conversation, topic):
    receipt = await RunService(state).submit(
        actor, conversation, topic, uuid4(), "Synthetic MCP acceptance", Scope.INTERACTIVE
    )
    fence = await RunRepository(state).claim(receipt.run_id, uuid4())
    assert fence is not None
    return fence


async def raw_call(url, token, name, args=None, list_tools=False):
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + token},
        timeout=10,
        follow_redirects=False,
        trust_env=False,
    ) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1], read_timeout_seconds=10) as session:
                await session.initialize()
                if list_tools:
                    return await session.list_tools()
                return await session.call_tool(name, args or {})


def payload():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "InsightPilot fixture", "version": "1"},
        },
    }


def test_official_http_client_four_tools_and_no_identity_in_schema(mcp_process):
    dep, actors, (conversation, a, b), settings, url, pid = mcp_process
    actor = actors["A_admin"]
    assert pid != os.getpid()

    async def check():
        async with state_pool(dep) as state:
            fence = await begin(state, actor, conversation, a)
            issuer = ToolTokenIssuer(state, settings.mcp_signing_key)
            token = await issuer.issue(fence, "list_datasets")
            tools = await raw_call(url, token, "", list_tools=True)
            assert {tool.name for tool in tools.tools} == {
                "list_datasets",
                "get_metric_definition",
                "validate_query",
                "execute_query",
            }
            for tool in tools.tools:
                encoded = str(tool.input_schema).lower()
                assert not any(
                    name in encoded
                    for name in (
                        "tenant_id",
                        "user_id",
                        "dsn",
                        "auth_version",
                        "execution_version",
                        "owner",
                    )
                )
            catalog = await raw_call(url, token, "list_datasets")
            assert not catalog.is_error and len(catalog.structured_content["views"]) == 6
            metric = await raw_call(
                url,
                await issuer.issue(fence, "get_metric_definition"),
                "get_metric_definition",
                {"metric_id": "paid_sales"},
            )
            assert metric.structured_content["grain"] == "order"
            ticket = await raw_call(
                url,
                await issuer.issue(fence, "validate_query"),
                "validate_query",
                {"sql": "SELECT COUNT(*) AS order_count FROM analysis.orders", "params": {}},
            )
            result = await raw_call(
                url,
                await issuer.issue(fence, "execute_query"),
                "execute_query",
                {"query_id": ticket.structured_content["query_id"]},
            )
            assert not result.is_error and result.structured_content["count"] == 1

    run(check())


@pytest.mark.parametrize(
    "variant,status",
    [
        ("none", 401),
        ("aud", 401),
        ("expired", 401),
        ("tampered", 401),
        ("scope", 403),
        ("ttl", 401),
    ],
)
def test_http_bearer_issuer_audience_time_and_scope_are_enforced(mcp_process, variant, status):
    dep, actors, (conversation, a, b), settings, url, pid = mcp_process

    async def issue():
        async with state_pool(dep) as state:
            fence = await begin(state, actors["A_admin"], conversation, a)
            return await ToolTokenIssuer(state, settings.mcp_signing_key).issue(
                fence, "list_datasets"
            )

    token = run(issue())
    claims = jwt.decode(token, options={"verify_signature": False})
    if variant == "aud":
        claims["aud"] = "https://another.invalid/mcp"
    if variant == "expired":
        claims["exp"] = int(time.time()) - 1
    if variant == "scope":
        claims["scopes"] = []
    if variant == "ttl":
        claims["exp"] = claims["iat"] + 3600
    token = jwt.encode(claims, settings.mcp_signing_key.get_secret_value(), algorithm="HS256")
    if variant == "tampered":
        head, body, signature = token.split(".")
        token = head + "." + body + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    headers = {"Accept": "application/json,text/event-stream"}
    if variant != "none":
        headers["Authorization"] = "Bearer " + token
    with httpx.Client(trust_env=False, timeout=5) as http:
        response = http.post(url, headers=headers, json=payload())
    assert response.status_code == status


def test_revoked_after_token_issuance_and_forged_tenant_arguments_fail(mcp_process):
    dep, actors, (conversation, a, b), settings, url, pid = mcp_process
    actor = actors["A_admin"]

    async def check():
        async with state_pool(dep) as state:
            fence = await begin(state, actor, conversation, a)
            token = await ToolTokenIssuer(state, settings.mcp_signing_key).issue(
                fence, "list_datasets"
            )
            forged = await raw_call(
                url, token, "list_datasets", {"tenant_id": str(actors["B_admin"].tenant_id)}
            )
            assert forged.is_error
            with psycopg.connect(dsn(dep, "postgres"), autocommit=True) as admin:
                admin.execute(
                    "UPDATE app_state.users SET enabled=false WHERE id=%s", (actor.user_id,)
                )
            async with httpx2.AsyncClient(trust_env=False, timeout=5) as http:
                response = await http.post(
                    url,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/json,text/event-stream",
                    },
                    json=payload(),
                )
                assert response.status_code == 401

    run(check())


def test_data_tools_client_and_concurrent_identities_do_not_share_context(mcp_process):
    dep, actors, (conversation, a, b), settings, url, pid = mcp_process
    cb, tb, _ = make_conversation(dep, actors["B_admin"])

    async def check():
        async with state_pool(dep) as state:
            client = DataToolsClient(settings, state)
            fa = await begin(state, actors["A_admin"], conversation, a)
            fb = await begin(state, actors["B_admin"], cb, tb)

            async def query(fence, label):
                catalog = await client.call(fence, "list_datasets", {})
                assert catalog.ok and len(catalog.data["views"]) == 6
                ticket = await client.call(
                    fence,
                    "validate_query",
                    {
                        "sql": "SELECT customer_ref FROM analysis.orders ORDER BY order_id LIMIT 5",
                        "params": {},
                    },
                )
                assert ticket.ok
                result = await client.call(
                    fence, "execute_query", {"query_id": ticket.data["query_id"]}
                )
                assert result.ok and result.data["count"] == 5
                assert all(row["customer_ref"].startswith(label) for row in result.data["rows"])

            await asyncio.gather(query(fa, "SYN-A-"), query(fb, "SYN-B-"))

    run(check())


def test_unavailable_mcp_returns_safe_domain_failure(mcp_process):
    from insightpilot.errors import DomainError

    dep, actors, (conversation, a, b), settings, url, pid = mcp_process
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    try:
        unavailable = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"

        async def check():
            async with state_pool(dep) as state:
                fence = await begin(state, actors["A_admin"], conversation, a)
                client = DataToolsClient(
                    settings.model_copy(update={"mcp_url": unavailable}), state
                )
                with pytest.raises(DomainError) as failed:
                    await client.call(fence, "list_datasets", {})
                assert failed.value.code == "mcp_unavailable" and failed.value.status == 503

        run(check())
    finally:
        listener.close()


def test_token_issuance_database_unavailable_is_safe_domain_failure(mcp_process):
    from insightpilot.errors import DomainError

    dep, actors, (conversation, a, b), settings, url, pid = mcp_process
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    try:
        refused_port = listener.getsockname()[1]

        async def check():
            async with state_pool(dep) as state:
                fence = await begin(state, actors["A_admin"], conversation, a)
            async with AsyncConnectionPool(
                f"host=127.0.0.1 port={refused_port} "
                "user=ip_state password=synthetic-refused dbname=insightpilot_fixture",
                open=False,
                min_size=0,
                max_size=1,
                timeout=0.2,
                reconnect_timeout=0.2,
                kwargs={"autocommit": True, "row_factory": dict_row, "connect_timeout": 1},
            ) as unavailable:
                client = DataToolsClient(settings, unavailable)
                with pytest.raises(DomainError) as failed:
                    await client.call(fence, "list_datasets", {})
                assert failed.value.code == "database_unavailable" and failed.value.status == 503

        run(check())
    finally:
        listener.close()
