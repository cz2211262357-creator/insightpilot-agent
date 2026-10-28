from uuid import UUID

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from insightpilot.auth.routes import current_actor
from insightpilot.contracts import ConversationItem, ConversationView, HistoryView, RunView
from insightpilot.runs.models import RunReceipt, Scope, TopicSnapshot
from insightpilot.runs.service import RunService


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationBody(Input):
    title: str = Field(default="新分析", min_length=1, max_length=80)


class TopicBody(Input):
    title: str = Field(min_length=1, max_length=80)
    expected_epoch: int = Field(ge=0)


class SwitchBody(Input):
    expected_epoch: int = Field(ge=0)


class MessageBody(Input):
    topic_id: UUID
    client_message_id: UUID
    question: str = Field(min_length=1, max_length=4000)
    scope: Scope = Scope.BACKGROUND
    expected_epoch: int | None = Field(default=None, ge=0)


class CancelBody(Input):
    expected_revision: int = Field(ge=1)


class ReviseBody(CancelBody):
    client_message_id: UUID
    question: str = Field(min_length=1, max_length=4000)


def service(request):
    return RunService(request.app.state.pools.postgres["state"])


def install_runs(app):
    @app.post("/api/v1/conversations", status_code=201, response_model=TopicSnapshot)
    async def create_conversation(body: ConversationBody, request: Request):
        return await service(request).create_conversation(await current_actor(request), body.title)

    @app.get("/api/v1/conversations", response_model=list[ConversationItem])
    async def conversations(request: Request):
        return await service(request).list_conversations(await current_actor(request))

    @app.get("/api/v1/conversations/{conversation_id}", response_model=ConversationView)
    async def conversation(conversation_id: UUID, request: Request):
        return await service(request).conversation(await current_actor(request), conversation_id)

    @app.post(
        "/api/v1/conversations/{conversation_id}/topics",
        status_code=201,
        response_model=TopicSnapshot,
    )
    async def create_topic(conversation_id: UUID, body: TopicBody, request: Request):
        return await service(request).create_topic(
            await current_actor(request), conversation_id, body.title, body.expected_epoch
        )

    @app.post(
        "/api/v1/conversations/{conversation_id}/topics/{topic_id}/activate",
        response_model=TopicSnapshot,
    )
    async def activate(conversation_id: UUID, topic_id: UUID, body: SwitchBody, request: Request):
        return await service(request).switch_topic(
            await current_actor(request), conversation_id, topic_id, body.expected_epoch
        )

    @app.post(
        "/api/v1/conversations/{conversation_id}/messages",
        status_code=202,
        response_model=RunReceipt,
    )
    async def submit(conversation_id: UUID, body: MessageBody, request: Request):
        return await service(request).submit(
            await current_actor(request),
            conversation_id,
            body.topic_id,
            body.client_message_id,
            body.question,
            body.scope,
            expected_epoch=body.expected_epoch,
        )

    @app.get("/api/v1/topics/{topic_id}/history", response_model=HistoryView)
    async def history(topic_id: UUID, request: Request):
        history = await service(request).topic_history(await current_actor(request), topic_id)
        history["runs"] = [item.model_copy(update={"report": None}) for item in history["runs"]]
        return history

    @app.get("/api/v1/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: UUID, request: Request):
        return (await service(request).get(await current_actor(request), run_id)).model_copy(
            update={"report": None}
        )

    @app.post("/api/v1/runs/{run_id}/cancel", response_model=RunView)
    async def cancel(run_id: UUID, body: CancelBody, request: Request):
        return await service(request).cancel(
            await current_actor(request), run_id, body.expected_revision
        )

    @app.post("/api/v1/runs/{run_id}/revise", status_code=202, response_model=RunReceipt)
    async def revise(run_id: UUID, body: ReviseBody, request: Request):
        return await service(request).revise(
            await current_actor(request),
            run_id,
            body.expected_revision,
            body.client_message_id,
            body.question,
        )
