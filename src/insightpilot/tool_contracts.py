"The only model-visible tool inputs; identity and execution authority are excluded."

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from insightpilot.catalog.models import Catalog, MetricDefinition
from insightpilot.query.models import EvidenceResult, QueryTicket


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyArgs(Input):
    pass


class MetricArgs(Input):
    metric_id: str = Field(min_length=1, max_length=64)


class QueryArgs(Input):
    sql: str = Field(min_length=1, max_length=16000)
    params: dict[str, StrictStr | StrictInt | StrictBool | None] = Field(max_length=32)


class ExecuteArgs(Input):
    query_id: UUID
    approval_id: UUID | None = None


ARGUMENTS = {
    "list_datasets": EmptyArgs,
    "get_metric_definition": MetricArgs,
    "validate_query": QueryArgs,
    "execute_query": ExecuteArgs,
}
RESULTS = {
    "list_datasets": Catalog,
    "get_metric_definition": MetricDefinition,
    "validate_query": QueryTicket,
    "execute_query": EvidenceResult,
}


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ok: bool
    data: dict | None = None
    error: dict | None = None
