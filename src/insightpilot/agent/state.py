from typing import Literal, TypedDict

from pydantic import Field

from insightpilot.catalog.models import Catalog
from insightpilot.reports.models import DTO, ChartSpec
from insightpilot.types import JsonScalar

MetricId = Literal[
    "paid_sales",
    "net_sales",
    "order_count",
    "aftersale_rate",
    "refund_amount",
    "avg_resolution_hours",
]


class QueryStep(DTO):
    id: str = Field(pattern=r"^s[1-4]$")
    goal: str = Field(min_length=1, max_length=240)
    metric_ids: list[MetricId] = Field(min_length=1, max_length=6)
    result_kind: Literal["aggregate", "detail"] = "aggregate"
    chart: ChartSpec = Field(default_factory=ChartSpec)


class PlannedAnalysis(DTO):
    needs_clarification: bool
    clarification_question: str | None = None
    metric_ids: list[MetricId] = Field(max_length=6)
    steps: list[QueryStep] = Field(max_length=4)


class GeneratedQuery(DTO):
    sql: str = Field(min_length=1, max_length=16000)
    params: dict[str, JsonScalar] = Field(max_length=32)


class PlanningInput(DTO):
    question: str
    catalog: Catalog
    history: list[dict]
    clarifications: list[str]


class SqlContext(DTO):
    question: str
    catalog: Catalog
    definitions: list[dict]
    clarifications: list[str]
    previous_sql: str | None = None
    error_code: str | None = None


class AgentState(TypedDict, total=False):
    question: str
    review_nonce: str
    catalog: dict
    history: list[dict]
    clarifications: list[str]
    plan: dict
    step_index: int
    generated: dict
    query_id: str
    requires_approval: bool
    approval_id: str | None
    evidence_ids: list[str]
    charts: dict
    error_code: str | None
    report: dict
    status: str
