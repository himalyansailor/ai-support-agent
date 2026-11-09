from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class OrgLinksOut(BaseModel):
    grafana: str
    langfuse: str | None  # None unless LLM tracing to Langfuse is on, with a host and project id


class AdminOrgOut(BaseModel):
    id: UUID
    name: str
    slug: str
    member_count: int
    created_at: datetime
    conversations_30d: int
    tokens_30d: int
    cost_micros_30d: int | None  # sum over priced turns; None when none is priced
    unpriced_turns_30d: int
    last_activity_at: datetime | None  # the latest event of a non-test conversation
    rollup_lagging: bool  # the rollup job is behind for this organisation (supportcore.analytics.queries.RollupState)
    rolled_through: datetime | None  # every hour before it is rolled up; None: never rolled up
    links: OrgLinksOut
    plan_name: str | None  # None: no plan chosen
    billing_status: str  # the effective status: no_plan, trialing, trial_expired, active, past_due, canceled, incomplete, dev


class OrgCountsOut(BaseModel):
    total: int
    active_7d: int
    active_30d: int


class LlmOut(BaseModel):
    tokens_30d: int
    cost_micros_30d: int | None
    unpriced_turns_30d: int


class SystemOut(BaseModel):
    error_rate_5m: float | None
    p50_ms: float | None
    p95_ms: float | None
    queue_depth: float | None
    provider_failure_rate_1h: dict[str, float] | None
    metrics_unavailable: bool


class OverviewLinksOut(BaseModel):
    grafana: str
    jaeger: str
    langfuse: str | None  # None unless LLM tracing to Langfuse is on (SUPPORT_LLM_TRACER=langfuse, configured)


class AdminRollupOut(BaseModel):
    """Organisations whose analytics rollup is behind (is the beat/rollup job running?)."""

    lagging_orgs: int
    oldest_live_from: datetime | None  # the earliest hour from which a lagging organisation is computed live


class RevenueOut(BaseModel):
    mrr_minor: int  # monthly recurring revenue, minor units of `currency`
    currency: str  # lowercase ISO code, e.g. "usd"
    paying_orgs: int  # active or past_due
    trialing_orgs: int


class AdminOverviewOut(BaseModel):
    orgs: OrgCountsOut
    conversations: dict[str, int]  # {"7d": n, "30d": n}
    llm: LlmOut
    system: SystemOut
    links: OverviewLinksOut
    rollup: AdminRollupOut
    revenue: RevenueOut
