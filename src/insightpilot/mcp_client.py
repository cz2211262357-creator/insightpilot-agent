"Only the fixed local resource server is reachable; no user/model URL or bearer input."

import asyncio

import anyio
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from insightpilot.auth.tool_tokens import ToolTokenIssuer
from insightpilot.errors import DomainError
from insightpilot.tool_contracts import ARGUMENTS, RESULTS, ToolResult


def transport_failure(error):
    if isinstance(error, ExceptionGroup):
        return all(transport_failure(child) for child in error.exceptions)
    return isinstance(
        error,
        (
            httpx2.HTTPError,
            MCPError,
            TimeoutError,
            OSError,
            anyio.BrokenResourceError,
            anyio.EndOfStream,
        ),
    )


class DataToolsClient:
    def __init__(self, settings, pool):
        self.settings = settings
        self.issuer = ToolTokenIssuer(pool, settings.mcp_signing_key)

    async def call(self, fence, name, arguments):
        if name not in ARGUMENTS:
            raise DomainError("tool_not_allowed", 422)
        try:
            validated = ARGUMENTS[name].model_validate(arguments)
        except ValidationError:
            raise DomainError("tool_arguments_invalid", 422) from None
        issued = await self.issuer.issue_bundle(fence, name)
        seconds = min(10, issued.seconds_remaining)
        try:
            async with asyncio.timeout(seconds):
                async with httpx2.AsyncClient(
                    headers={"Authorization": "Bearer " + issued.bearer.get_secret_value()},
                    timeout=seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as http:
                    async with streamable_http_client(
                        self.settings.mcp_url, http_client=http, max_sse_event_size=2097152
                    ) as streams:
                        async with ClientSession(
                            streams[0], streams[1], read_timeout_seconds=seconds
                        ) as session:
                            await session.initialize()
                            result = await session.call_tool(
                                name, validated.model_dump(mode="json", exclude_none=True)
                            )
            if result.is_error:
                error = (result.structured_content or {}).get("error")
                if not isinstance(error, dict) or not {"code", "message", "status"} <= error.keys():
                    raise DomainError("mcp_invalid_result", 503)
                return ToolResult(ok=False, error=error)
            if result.structured_content is None:
                raise DomainError("mcp_invalid_result", 503)
            data = RESULTS[name].model_validate(result.structured_content).model_dump(mode="json")
            return ToolResult(ok=True, data=data)
        except (TimeoutError, httpx2.HTTPError):
            raise DomainError("mcp_unavailable", 503) from None
        except ValidationError:
            raise DomainError("mcp_invalid_result", 503) from None
        except ExceptionGroup as error:
            if not transport_failure(error):
                raise
            raise DomainError("mcp_unavailable", 503) from None
