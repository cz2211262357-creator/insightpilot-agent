from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    metric_id: str
    name: str
    grain: str
    definition: str
    time_column: str
    timezone: str = "Asia/Shanghai"
    empty_result: str


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = "2026-10-v1"
    dataset_version: UUID
    as_of: datetime | None = None
    views: dict[str, list[str]]
    metrics: list[MetricDefinition]


@dataclass(frozen=True)
class DatasetVersion:
    id: UUID
    tenant_ids: tuple[UUID, UUID]
    sha256: str
