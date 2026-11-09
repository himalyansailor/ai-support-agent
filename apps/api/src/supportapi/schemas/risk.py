from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# Bounds here only limit request size; RiskRuleService validates (with stable error codes).
class CreateRiskRuleRequest(BaseModel):
    name: str = Field(max_length=1000)
    description: str = Field(default="", max_length=5000)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    action: str = Field(default="escalate", max_length=50)


class UpdateRiskRuleRequest(BaseModel):
    """Partial: absent fields stay as they are."""

    name: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=5000)
    keywords: list[str] | None = Field(default=None, max_length=100)
    action: str | None = Field(default=None, max_length=50)
    enabled: bool | None = None
    params: dict[str, Any] | None = None


class ReorderRiskRulesRequest(BaseModel):
    ids: list[UUID] = Field(max_length=200)


class RiskRuleOut(BaseModel):
    id: UUID
    kind: str
    builtin_key: str | None
    name: str
    description: str
    keywords: list[str]
    action: str
    enabled: bool
    locked: bool
    position: int
    params: dict[str, Any]
