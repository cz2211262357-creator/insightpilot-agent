from uuid import UUID

from fastapi import Request
from pydantic import BaseModel, ConfigDict, SecretStr

from insightpilot.auth.routes import current_actor
from insightpilot.contracts import ApprovalView, ClarificationView, QueryReviewView
from insightpilot.query.confirmations import ConfirmationService
from insightpilot.runs.confirmations import ConfirmationService as RunConfirmations
from insightpilot.runs.confirmations import ResumeKey
from insightpilot.runs.models import RunReceipt


class ApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: SecretStr


class ResumeBody(ResumeKey):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    confirmation_id: UUID
    decision: dict


def install_query(app):
    def confirmations(request):
        return ConfirmationService(
            request.app.state.pools.postgres["state"], request.app.state.settings.mcp_signing_key
        )

    @app.get(
        "/api/v1/runs/{run_id}/confirmation", response_model=ClarificationView | QueryReviewView
    )
    async def pending(run_id: UUID, request: Request):
        return await RunConfirmations(
            request.app.state.pools.postgres["state"], request.app.state.settings.mcp_signing_key
        ).issue(await current_actor(request), run_id)

    @app.post("/api/v1/confirmations/{confirmation_id}/approve", response_model=ApprovalView)
    async def approve(confirmation_id: UUID, body: ApprovalBody, request: Request):
        return await confirmations(request).approve(
            await current_actor(request), confirmation_id, body.token.get_secret_value()
        )

    @app.post("/api/v1/runs/{run_id}/resume", status_code=202, response_model=RunReceipt)
    async def resume(run_id: UUID, body: ResumeBody, request: Request):
        return await RunConfirmations(
            request.app.state.pools.postgres["state"], request.app.state.settings.mcp_signing_key
        ).resume(
            await current_actor(request),
            run_id,
            body.confirmation_id,
            body.decision,
            ResumeKey(idempotency_key=body.idempotency_key, token=body.token),
        )
