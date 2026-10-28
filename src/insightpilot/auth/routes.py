from uuid import UUID

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from insightpilot.auth.service import AuthService
from insightpilot.contracts import ActorView, MemberView
from insightpilot.types import Role


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    tenant_id: UUID
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=256)


class MemberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    role: Role | None = None


def service(request):
    return AuthService(request.app.state.pools.postgres["state"], request.app.state.pools.redis)


async def current_actor(request: Request):
    return await service(request).authenticate(request.cookies.get("ip_session"))


def install_auth(app):
    @app.post("/api/v1/auth/login", response_model=ActorView)
    async def login(body: LoginBody, request: Request, response: Response):
        actor, token, csrf = await service(request).login(
            body.tenant_id, body.email, body.password.get_secret_value()
        )
        previous = request.cookies.get("ip_session")
        if previous and previous != token:
            await service(request).logout(previous)
        secure = request.app.state.settings.environment == "production"
        response.set_cookie(
            "ip_session",
            token,
            max_age=28800,
            httponly=True,
            secure=secure,
            samesite="strict",
            path="/",
        )
        response.set_cookie(
            "ip_csrf",
            csrf,
            max_age=28800,
            httponly=False,
            secure=secure,
            samesite="strict",
            path="/",
        )
        return service(request).public_actor(actor)

    @app.get("/api/v1/auth/me", response_model=ActorView)
    async def me(request: Request):
        return service(request).public_actor(await current_actor(request))

    @app.post("/api/v1/auth/logout", status_code=204)
    async def logout(request: Request):
        await service(request).logout(request.cookies.get("ip_session"))
        response = Response(status_code=204)
        response.delete_cookie("ip_session", path="/")
        response.delete_cookie("ip_csrf", path="/")
        return response

    @app.get("/api/v1/members/{user_id}", response_model=MemberView)
    async def member(user_id: UUID, request: Request):
        return await service(request).member(await current_actor(request), user_id)

    @app.patch("/api/v1/members/{user_id}", response_model=MemberView)
    async def change_member(user_id: UUID, body: MemberBody, request: Request):
        return await service(request).change_member(
            await current_actor(request),
            user_id,
            body.enabled,
            body.role.value if body.role else None,
        )
