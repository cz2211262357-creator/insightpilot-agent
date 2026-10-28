from pydantic import BaseModel, ConfigDict

LIMITS = {"model": ("model_used", 8), "query": ("query_used", 4), "repair": ("repair_used", 2)}
ACTIVE_SECONDS = {"INTERACTIVE": 120, "BACKGROUND": 300}
QUEUE_SECONDS = {"INTERACTIVE": 60, "BACKGROUND": 600}


class RemainingBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_used: int
    query_used: int
    repair_used: int
    remaining_seconds: float
    model_timeout_seconds: float
