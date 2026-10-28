from uuid import UUID

from pydantic import BaseModel, ConfigDict

from insightpilot.types import JsonScalar


class ValidatedSql(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    sql: str
    params: dict[str, JsonScalar]
    sql_hash: str
    views: list[str]
    columns: list[str]
    dataset_version: UUID
    catalog_version: str
    policy_version: str = "2026-10-v1"


class QueryTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query_id: UUID
    sql_hash: str
    requires_approval: bool
    explain: dict


class EvidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_id: UUID
    query_id: UUID
    columns: list[str]
    types: dict[str, str]
    rows: list[dict[str, JsonScalar]]
    count: int
    truncated: bool
    sha256: str
    metric_ids: list[str]
    dataset_version: UUID
    sql_hash: str
