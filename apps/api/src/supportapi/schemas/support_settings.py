from typing import Any

from pydantic import BaseModel, Field


class SupportSettingsOut(BaseModel):
    timezone: str
    weekly_hours: dict[str, list[list[str]]]
    holidays: list[str]
    sla_targets: dict[str, dict[str, int]]


# Shapes are validated by SupportSettingsService (stable error codes); bounds here only limit request size.
class SupportSettingsRequest(BaseModel):
    timezone: str = Field(max_length=200)
    weekly_hours: dict[str, Any]
    holidays: list[Any] = Field(max_length=400)
    sla_targets: dict[str, Any]
