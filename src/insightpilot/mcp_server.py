"MCP resource server with real HTTP auth, per-call context, and four readonly tools."

from contextlib import asynccontextmanager
from uuid import UUID

import psycopg
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.tools import Tool
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from psycopg_pool import PoolTimeout
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from insightpilot.auth.tool_tokens import (
    AUDIENCE,
    BASE_SCOPE,
    ISSUER,
    TOOL_SCOPES,
    DataTokenVerifier,
    ToolClaims,
)
from insightpilot.db import DatabasePools
from insightpilot.errors import DomainError
from insightpilot.query.gateway import QueryGateway
from insightpilot.query.repository import authorized_scope
from insightpilot.tool_contracts import ARGUMENTS


def error_result(error):
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=error.code)],
        structuredContent={
            "error": {"code": error.code, "status": error.status, "message": error.message}
        },
    )


def trusted_claims():
    token = get_access_token()
    if token is None or token.claims is None:
        raise DomainError("authentication_required", 401)
    return ToolClaims.model_validate(token.claims)


def create_mcp_app(settings):
    pools = DatabasePools(settings)
    gateway = QueryGateway(
        pools.postgres["state"], pools.postgres["query"], settings.mcp_signing_key
    )

    async def guarded_call(name, operation):
        try:
            claims = trusted_claims()
            if TOOL_SCOPES[name] not in claims.scopes:
                raise DomainError("tool_scope_required", 403)
            data = await operation(claims.actor(), claims.fence())
            return CallToolResult(
                content=[TextContent(type="text", text="Structured result attached")],
                structuredContent=data,
            )
        except DomainError as error:
            return error_result(error)
        except (psycopg.Error, PoolTimeout):
            return error_result(DomainError("database_unavailable", 503))

    async def list_datasets() -> CallToolResult:
        async def operation(actor, fence):
            async with pools.postgres["state"].connection() as conn, conn.transaction():
                _, catalog = await authorized_scope(conn, actor, fence)
                return catalog.model_dump(mode="json")

        return await guarded_call("list_datasets", operation)

    async def get_metric_definition(metric_id: str) -> CallToolResult:
        async def operation(actor, fence):
            async with pools.postgres["state"].connection() as conn, conn.transaction():
                _, catalog = await authorized_scope(conn, actor, fence)
                for metric in catalog.metrics:
                    if metric.metric_id == metric_id:
                        return metric.model_dump(mode="json")
            raise DomainError("metric_not_found", 404)

        return await guarded_call("get_metric_definition", operation)

    async def validate_query(sql: str, params: dict) -> CallToolResult:
        async def operation(actor, fence):
            return (await gateway.validate(actor, fence, sql, params)).model_dump(mode="json")

        return await guarded_call("validate_query", operation)

    async def execute_query(query_id: UUID, approval_id: UUID | None = None) -> CallToolResult:
        async def operation(actor, fence):
            return (await gateway.execute(actor, fence, query_id, approval_id)).model_dump(
                mode="json"
            )

        return await guarded_call("execute_query", operation)

    async def argument_boundary(ctx, call_next):
        if ctx.method == "tools/call":
            try:
                params = ctx.params or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if name not in ARGUMENTS:
                    raise DomainError("tool_not_allowed", 422)
                ARGUMENTS[name].model_validate(arguments)
                if TOOL_SCOPES[name] not in trusted_claims().scopes:
                    raise DomainError("tool_scope_required", 403)
            except ValidationError:
                return error_result(DomainError("tool_arguments_invalid", 422))
            except DomainError as error:
                return error_result(error)
        return await call_next(ctx)

    tools = []
    for callback in (list_datasets, get_metric_definition, validate_query, execute_query):
        tool = Tool.from_function(
            callback,
            structured_output=False,
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=callback.__name__ != "validate_query",
            ),
        )
        tool.parameters = ARGUMENTS[callback.__name__].model_json_schema()
        tools.append(tool)
    server = MCPServer(
        "InsightPilot data tools",
        tools=tools,
        middleware=[argument_boundary],
        token_verifier=DataTokenVerifier(pools.postgres["state"], settings.mcp_signing_key),
        auth=AuthSettings(
            issuer_url=ISSUER,
            resource_server_url=AUDIENCE,
            required_scopes=[BASE_SCOPE],
            validate_token_resource=True,
        ),
    )
    mcp_app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=262144,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "mcp:8011"],
            allowed_origins=["http://localhost:8090", "http://127.0.0.1:8090"],
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        await pools.open()
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            await pools.close()

    async def live(request):
        return JSONResponse({"status": "alive"})

    async def ready(request):
        health = await pools.health()
        return JSONResponse(
            {"status": "ready" if all(health.values()) else "unavailable", "dependencies": health},
            status_code=200 if all(health.values()) else 503,
        )

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/api/v1/health/live", live),
            Route("/api/v1/health/ready", ready),
            Mount("/", app=mcp_app),
        ],
    )
