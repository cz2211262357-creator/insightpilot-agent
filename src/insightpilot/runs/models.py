from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Scope(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    BACKGROUND = "BACKGROUND"


class RunReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    message_id: UUID
    run_id: UUID
    topic_id: UUID
    conversation_id: UUID
    task_revision: int
    epoch: int
    scope: Scope


class TopicSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conversation_id: UUID
    active_topic_id: UUID
    epoch: int


class RunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    topic_id: UUID
    conversation_id: UUID
    question: str
    dataset_version: UUID
    created_at: datetime
    snapshot_seq: int = 0
    execution_version: int = 0
    public_plan: list[dict] = []
    report_error_code: str | None = None
    has_report: bool = False
    task_revision: int
    state: str
    scope: Scope
    epoch: int
    model_used: int
    query_used: int
    repair_used: int
    attempts: int
    report: dict | None = None
    error_code: str | None = None
