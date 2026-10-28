"Internal service capabilities; no browser endpoint issues these bearer tokens."

import time
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

import jwt
import psycopg
from mcp.server.auth.provider import AccessToken
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from starlette.exceptions import HTTPException

from insightpilot.auth.service import require_current
from insightpilot.errors import DomainError
from insightpilot.runs.repository import require_fence
from insightpilot.types import Actor, Role, RunFence

ISSUER = "https://insightpilot.local/"
AUDIENCE = "https://insightpilot.local/mcp"
BASE_SCOPE = "insightpilot:read"
TOOL_SCOPES = {
    name: "tool:" + name
    for name in ("list_datasets", "get_metric_definition", "validate_query", "execute_query")
}
ALLOWED_SCOPES = {BASE_SCOPE, *TOOL_SCOPES.values()}


class ToolClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    iss: Literal[ISSUER]
    aud: Literal[AUDIENCE]
    sub: UUID
    tenant_id: UUID
    role: Role
    auth_version: int = Field(gt=0, strict=True)
    run_id: UUID
    topic_id: UUID
    task_revision: int = Field(gt=0, strict=True)
    execution_version: int = Field(gt=0, strict=True)
    owner: UUID
    iat: int = Field(strict=True)
    nbf: int = Field(strict=True)
    exp: int = Field(strict=True)
    jti: UUID
    scopes: list[str]

    def actor(self):
        return Actor(self.tenant_id, self.sub, self.role, self.auth_version)

    def fence(self):
        return RunFence(
            self.tenant_id,
            self.run_id,
            self.topic_id,
            self.task_revision,
            self.execution_version,
            self.owner,
            self.auth_version,
        )


@dataclass(frozen=True)
class IssuedToken:
    bearer: SecretStr
    seconds_remaining: float


class ToolTokenIssuer:
    def __init__(self, pool, key):
        self.pool = pool
        self.key = key

    async def _issue_bundle(self, fence, tool_name):
        if tool_name not in TOOL_SCOPES:
            raise DomainError("tool_not_allowed", 422)
        async with self.pool.connection() as conn, conn.transaction():
            await require_fence(conn, fence)
            row = await (
                await conn.execute(
                    """
                SELECT r.user_id,u.role,
                  EXTRACT(EPOCH FROM(r.active_deadline-clock_timestamp())) AS remaining
                FROM app_state.runs r JOIN app_state.users u
                  ON u.id=r.user_id AND u.tenant_id=r.tenant_id WHERE r.id=%s
                """,
                    (fence.run_id,),
                )
            ).fetchone()
        if row is None or row["remaining"] <= 0:
            raise DomainError("stale_execution", 409)
        now = int(time.time())
        claims = ToolClaims(
            iss=ISSUER,
            aud=AUDIENCE,
            sub=row["user_id"],
            tenant_id=fence.tenant_id,
            role=row["role"],
            auth_version=fence.auth_version,
            run_id=fence.run_id,
            topic_id=fence.topic_id,
            task_revision=fence.task_revision,
            execution_version=fence.execution_version,
            owner=fence.owner,
            iat=now,
            nbf=now,
            exp=now + 60,
            jti=uuid4(),
            scopes=[BASE_SCOPE, TOOL_SCOPES[tool_name]],
        )
        token = jwt.encode(
            claims.model_dump(mode="json"), self.key.get_secret_value(), algorithm="HS256"
        )
        return IssuedToken(SecretStr(token), float(row["remaining"]))

    async def issue_bundle(self, fence, tool_name):
        try:
            return await self._issue_bundle(fence, tool_name)
        except (psycopg.Error, PoolTimeout):
            raise DomainError("database_unavailable", 503) from None

    async def issue(self, fence, tool_name):
        return (await self.issue_bundle(fence, tool_name)).bearer.get_secret_value()


class DataTokenVerifier:
    def __init__(self, pool, key):
        self.pool = pool
        self.key = key

    async def verify_token(self, token):
        try:
            raw = jwt.decode(
                token,
                self.key.get_secret_value(),
                algorithms=["HS256"],
                issuer=ISSUER,
                audience=AUDIENCE,
                options={"require": ["iss", "aud", "sub", "iat", "nbf", "exp", "jti"]},
            )
            claims = ToolClaims.model_validate(raw)
            if (
                claims.exp - claims.iat != 60
                or claims.nbf != claims.iat
                or not set(claims.scopes) <= ALLOWED_SCOPES
            ):
                return None
            async with self.pool.connection() as conn, conn.transaction():
                pointer = await (
                    await conn.execute(
                        "SELECT user_id FROM app_state.runs WHERE tenant_id=%s AND id=%s",
                        (claims.tenant_id, claims.run_id),
                    )
                ).fetchone()
                if pointer is None or pointer["user_id"] != claims.sub:
                    return None
                await require_current(conn, claims.actor(), True)
                await require_fence(conn, claims.fence())
            return AccessToken(
                token=token,
                client_id="insightpilot-worker",
                scopes=claims.scopes,
                expires_at=claims.exp,
                resource=AUDIENCE,
                subject=str(claims.sub),
                claims=claims.model_dump(mode="json"),
            )
        except (jwt.PyJWTError, ValidationError, DomainError, ValueError, TypeError):
            return None
        except (psycopg.Error, PoolTimeout):
            raise HTTPException(503, detail="Authorization dependency unavailable") from None
