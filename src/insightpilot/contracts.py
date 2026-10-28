"Public read models; credentials and raw model responses are absent."

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from insightpilot.catalog.models import MetricDefinition
from insightpilot.query.models import EvidenceResult
from insightpilot.reports.models import ChartSpec, ReportClaim
from insightpilot.runs.models import RunSnapshot, TopicSnapshot
from insightpilot.types import JsonScalar, Role


class View(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(View):
    code: str
    message: str
    request_id: str


class ErrorEnvelope(View):
    error: ErrorDetail


class ActorView(View):
    tenant_id: UUID
    user_id: UUID
    role: Role
    auth_version: int


class MemberView(View):
    user_id: UUID
    email: str
    display_name: str
    role: Role
    enabled: bool
    auth_version: int


class TopicView(View):
    topic_id: UUID
    title: str
    created_at: datetime


class ConversationView(TopicSnapshot):
    topics: list[TopicView]


class ConversationItem(TopicSnapshot):
    created_at: datetime


class ReportView(View):
    summary: str
    claims: list[ReportClaim]
    evidence: list[EvidenceResult]
    charts: list[dict]
    chart_specs: dict[str, ChartSpec] = Field(default_factory=dict)
    warnings: list[str]
    catalog_version: str


class RunView(RunSnapshot):
    report: ReportView | None = None


class MessageView(View):
    id: UUID
    run_id: UUID | None
    role: Literal["user", "assistant"]
    status: Literal["completed", "partial", "cancelled", "failed"]
    content: str
    created_at: datetime


class HistoryView(View):
    messages: list[MessageView]
    runs: list[RunView]


class RunPage(View):
    items: list[RunView]
    next_cursor: UUID | None = None


class EvidenceView(View):
    evidence: EvidenceResult
    sql: str
    params: dict[str, JsonScalar]
    explain: dict
    catalog_version: str
    policy_version: str
    metric_definitions: list[MetricDefinition]


class ClarificationView(View):
    kind: Literal["clarification"]
    confirmation_id: UUID
    token: str
    run_id: UUID
    topic_id: UUID
    task_revision: int
    question: str
    budget: dict[str, int | str]
    expires_at: datetime


class QueryReviewView(View):
    kind: Literal["query_approval"]
    confirmation_id: UUID
    token: str
    run_id: UUID
    topic_id: UUID
    task_revision: int
    query_id: UUID
    sql: str
    params: dict[str, JsonScalar]
    explain: dict
    sql_hash: str
    budget: dict[str, int | str]
    expires_at: datetime
    catalog_version: str


class ApprovalView(View):
    accepted: bool
    run_id: UUID
    task_revision: int
    confirmation_id: UUID


class SystemView(View):
    model_name: str
    data_kind: Literal["synthetic"]
    model_concurrency: int
    background_concurrency: int
    model_calls: int
    query_calls: int
    repair_calls: int
    interactive_seconds: int
    background_seconds: int


ERROR_RESPONSES = {code: {"model": ErrorEnvelope} for code in (401, 403, 404, 409, 422, 429, 503)}
