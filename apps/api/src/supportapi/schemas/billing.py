from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from supportcore.billing.plans import PlanInput, PlanPatch

__all__ = [
    "AdminPlanOut",
    "BillingBannerOut",
    "BillingSummaryOut",
    "CapacityOut",
    "LimitBehaviourIn",
    "MeterOut",
    "PlanChoiceIn",
    "PlanIn",
    "PlanOut",
    "PlanPatchIn",
    "PlanPriceOut",
    "TrialIn",
    "UrlOut",
]

LimitBehaviour = Literal["hard_stop", "overage"]

PlanIn = PlanInput
PlanPatchIn = PlanPatch


class _PlanLimits(BaseModel):
    seats: int | None
    messages: int | None
    voice_minutes: int | None
    storage_mb: int | None
    knowledge_bases: int | None
    phone_lines: int | None
    messages_overage_micros: int | None
    voice_minutes_overage_micros: int | None


class PlanOut(_PlanLimits):
    """A plan as members see it when choosing one."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str
    name: str
    description: str
    monthly_price_minor: int
    currency: str
    trial_days: int
    sort_order: int


class PlanPriceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    amount_minor: int | None
    provider: str


class AdminPlanOut(PlanOut):
    active: bool
    sync_status: str
    sync_error: str | None
    synced_at: datetime | None
    prices: list[PlanPriceOut]


class MeterOut(BaseModel):
    meter: str
    used: int
    limit: int | None
    overage: bool
    reached: bool
    percent: int | None


class CapacityOut(BaseModel):
    used: int
    limit: int | None


class BillingSummaryOut(BaseModel):
    """The organisation's billing account for its billing page."""

    plan: PlanOut | None
    status: str
    effective_status: str
    locked: bool
    subscribed: bool
    limit_behaviour: str
    trial_started_at: datetime | None
    trial_ends_at: datetime | None
    period_start: datetime
    period_end: datetime
    cancel_at_period_end: bool
    supports_metered: bool
    grants_everything: bool
    currency: str
    meters: list[MeterOut]
    capacity: dict[str, CapacityOut]


class BillingBannerOut(BaseModel):
    effective_status: str
    locked: bool
    trial_ends_at: datetime | None
    reached: list[str]
    past_due: bool


class TrialIn(BaseModel):
    plan_id: UUID
    limit_behaviour: LimitBehaviour = "hard_stop"


class PlanChoiceIn(BaseModel):
    plan_id: UUID


class LimitBehaviourIn(BaseModel):
    limit_behaviour: LimitBehaviour


class UrlOut(BaseModel):
    url: str
