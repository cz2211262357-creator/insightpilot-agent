"HTTP boundaries and bounded dependency health; no provider request on a probe."

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from redis.exceptions import RedisError

from insightpilot.auth.routes import install_auth, service
from insightpilot.browser_routes import install_browser
from insightpilot.config import Settings
from insightpilot.contracts import ERROR_RESPONSES
from insightpilot.db import DatabasePools
from insightpilot.errors import DomainError
from insightpilot.query.routes import install_query
from insightpilot.runs.routes import install_runs


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pools = DatabasePools(settings)
        app.state.pools = pools
        await pools.open()
        worker_task = None
        if settings.process_role == "worker":
            from insightpilot.worker import AnalysisWorker

            worker_task = asyncio.create_task(AnalysisWorker(settings, pools).serve())
        app.state.worker_task = worker_task
        try:
            yield
        finally:
            if worker_task:
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            await pools.close()

    app = FastAPI(
        title="InsightPilot", version="0.1.0", lifespan=lifespan, responses=ERROR_RESPONSES
    )
    app.state.settings = settings

    def error_response(request, error):
        return JSONResponse(
            status_code=error.status,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "request_id": request.state.request_id,
                }
            },
        )

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return error_response(request, DomainError("invalid_request", 422))

    async def unavailable(request, error):
        return error_response(request, DomainError("dependency_unavailable", 503))

    for error_type in (OperationalError, PoolTimeout, RedisError):
        app.add_exception_handler(error_type, unavailable)

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        request.state.request_id = uuid4().hex
        try:
            if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith(
                "/api/v1/"
            ):
                if request.headers.get("origin") not in request.app.state.settings.web_origins:
                    raise DomainError("origin_rejected", 403)
                if request.url.path != "/api/v1/auth/login":
                    await service(request).check_csrf(
                        request.cookies.get("ip_session"), request.headers.get("x-csrf-token")
                    )
            response = await call_next(request)
        except DomainError as error:
            response = error_response(request, error)
        except (OperationalError, PoolTimeout, RedisError):
            response = error_response(request, DomainError("dependency_unavailable", 503))
        response.headers["cache-control"] = "no-store"
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["x-request-id"] = request.state.request_id
        return response

    @app.get("/api/v1/health/live")
    async def liveness():
        return {"status": "alive"}

    @app.get("/api/v1/health/ready")
    async def readiness(request: Request):
        status = await request.app.state.pools.health()
        if settings.process_role == "worker":
            task = request.app.state.worker_task
            status["worker"] = task is not None and not task.done()
        if not all(status.values()):
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "dependency_unavailable",
                        "message": "Dependencies unavailable",
                        "request_id": request.state.request_id,
                        "details": {"dependencies": status},
                    }
                },
            )
        return {"status": "ready", "dependencies": status}

    if settings.process_role == "api":
        install_auth(app)
        install_runs(app)
        install_query(app)
        install_browser(app)
    return app
