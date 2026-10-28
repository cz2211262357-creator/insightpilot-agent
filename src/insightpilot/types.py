"Server-owned execution identities; never filled from model tool arguments."

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

JsonScalar = str | int | bool | None


class Role(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"


@dataclass(frozen=True, slots=True)
class Actor:
    tenant_id: UUID
    user_id: UUID
    role: Role
    auth_version: int


@dataclass(frozen=True, slots=True)
class RunFence:
    tenant_id: UUID
    run_id: UUID
    topic_id: UUID
    task_revision: int
    execution_version: int
    owner: UUID
    auth_version: int
