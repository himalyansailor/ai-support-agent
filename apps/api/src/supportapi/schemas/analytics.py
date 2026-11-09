from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class Metrics(BaseModel):
    """Every metric of a range or bucket, with the derived rates and averages (null when the denominator is
    zero; `cost_micros` is the sum over its priced bot turns, null when it has none; `cost_unpriced_turns` counts the others)."""

    conversations_started: int
    entered_team: int
    resolved: int
    ai_resolved: int
    first_response_count: int
    first_response_seconds_sum: int
    resolution_count: int
    resolution_seconds_sum: int
    csat_count: int
    csat_sum: int
    csat_positive: int
    sla_breaches: int
    messages_customer: int
    messages_bot: int
    messages_agent: int
    bot_turns: int
    knowledge_gaps: int
    input_tokens: int
    output_tokens: int
    cost_micros: int | None
    cost_priced_turns: int
    cost_unpriced_turns: int
    ai_resolution_rate: float | None
    escalation_rate: float | None
    avg_first_response_seconds: float | None
    avg_resolution_seconds: float | None
    csat_avg: float | None
    csat_positive_share: float | None
    by_channel: dict[str, int] = {}  # conversations_started per channel (only channels with some; the channel filter applies)


class SeriesPoint(Metrics):
    bucket_start: datetime
    bucket_end: datetime


class OpenQueue(BaseModel):
    open: int
    pending: int
    unassigned: int
    bot: int


class RollupOut(BaseModel):
    """How far the stored hourly rollups reach. Hours before `live_from` are read from the rollups; from it on
    they are computed live. `rolled_through` is null when the organisation was never rolled up. `lagging` is true
    when the rollup job is behind (hours before `live_from` it has not stored read as zeros): is beat running?"""

    rolled_through: datetime | None
    live_from: datetime
    lagging: bool


class OverviewOut(BaseModel):
    from_date: str
    to_date: str
    interval: str
    timezone: str
    totals: Metrics
    series: list[SeriesPoint]
    open_queue: OpenQueue
    rollup: RollupOut


class AgentStatsOut(BaseModel):
    membership_id: UUID
    name: str | None  # null when `removed`, or when the user has no name
    removed: bool  # the membership no longer exists (its history stays)
    assigned: int
    handled: int
    avg_handle_seconds: float | None
    avg_first_response_seconds: float | None
    csat_avg: float | None
    csat_count: int


class AgentsOut(BaseModel):
    items: list[AgentStatsOut]


class ReasonOut(BaseModel):
    key: str
    label: str
    count: int


class ReasonsOut(BaseModel):
    kind: str
    items: list[ReasonOut]


class MessagesByRole(BaseModel):
    customer: int
    bot: int
    agent: int


class ModelUsageOut(BaseModel):
    provider: str | None
    model: str
    bot_turns: int
    input_tokens: int
    output_tokens: int
    cost_micros: int | None
    unpriced_turns: int


class UsageOut(BaseModel):
    messages: MessagesByRole
    bot_turns: int
    input_tokens: int
    output_tokens: int
    cost_micros: int | None
    unpriced_turns: int
    by_model: list[ModelUsageOut]


class KnowledgeGapOut(BaseModel):
    bot_turn_id: UUID
    conversation_id: UUID
    question_text: str | None
    kb_ids: list[UUID]
    kb_names: list[str]
    occurred_at: datetime
    best_confidence: float | None


class KnowledgeGapPage(BaseModel):
    items: list[KnowledgeGapOut]
    next_cursor: str | None
