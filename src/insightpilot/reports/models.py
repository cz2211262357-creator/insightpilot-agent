from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

Scalar = StrictStr | StrictInt | StrictBool | None


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChartSpec(DTO):
    kind: Literal["bar", "line", "table"] = "table"
    x_column: str | None = None
    y_column: str | None = None


class ReportClaim(DTO):
    evidence_id: UUID
    column: str = Field(min_length=1, max_length=63)
    row_index: int = Field(default=0, ge=0)
    operation: Literal["cell", "sum", "share"] = "cell"
    value: Scalar
    text_template: str = Field(default="{value}", min_length=1, max_length=240)
    denominator_evidence_id: UUID | None = None


class ReportNarrative(DTO):
    claims: list[ReportClaim] = Field(max_length=12)


class FactCell(DTO):
    evidence_id: UUID
    row_index: int
    column: str
    value: Scalar
    label: str
    unit: str


class ReportFacts(DTO):
    question: str
    cells: list[FactCell]
    warnings: list[str]


class Report(DTO):
    summary: str
    claims: list[dict]
    evidence: list[dict]
    charts: list[dict]
    chart_specs: dict[str, ChartSpec] = Field(default_factory=dict)
    warnings: list[str]
    catalog_version: str = "2026-10-v1"
