"""Risk rules: what makes the bot hand a conversation to a person (read: bot.configure or analytics.view)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    build_risk_rule_service,
    get_container,
    require_any_permission,
    require_permission,
)
from supportapi.schemas.risk import CreateRiskRuleRequest, ReorderRiskRulesRequest, RiskRuleOut, UpdateRiskRuleRequest
from supportcore.db.models import RiskRule
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["risk"])


def rule_out(rule: RiskRule) -> RiskRuleOut:
    return RiskRuleOut.model_validate(rule, from_attributes=True)


@router.get("/orgs/{org_id}/risk-rules", response_model=list[RiskRuleOut])
async def list_risk_rules(
    session: SessionDep,
    ctx: OrgContext = Depends(require_any_permission(Permission.BOT_CONFIGURE, Permission.ANALYTICS_VIEW)),
    container: Container = Depends(get_container),
) -> list[RiskRuleOut]:
    return [rule_out(r) for r in await build_risk_rule_service(session, ctx.organisation.id, container).list()]


@router.post("/orgs/{org_id}/risk-rules", response_model=RiskRuleOut, status_code=201)
async def create_risk_rule(
    session: SessionDep,
    body: CreateRiskRuleRequest,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> RiskRuleOut:
    service = build_risk_rule_service(session, ctx.organisation.id, container)
    return rule_out(await service.create_topic(body.name, body.description, body.keywords, body.action, ctx.user))


# Declared before the /{rule_id} routes so "order" is never read as an id.
@router.put("/orgs/{org_id}/risk-rules/order", response_model=list[RiskRuleOut])
async def reorder_risk_rules(
    session: SessionDep,
    body: ReorderRiskRulesRequest,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> list[RiskRuleOut]:
    service = build_risk_rule_service(session, ctx.organisation.id, container)
    return [rule_out(r) for r in await service.reorder(body.ids, ctx.user)]


@router.patch("/orgs/{org_id}/risk-rules/{rule_id}", response_model=RiskRuleOut)
async def update_risk_rule(
    session: SessionDep,
    rule_id: UUID,
    body: UpdateRiskRuleRequest,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> RiskRuleOut:
    service = build_risk_rule_service(session, ctx.organisation.id, container)
    return rule_out(await service.update(rule_id, actor=ctx.user, **body.model_dump(exclude_unset=True)))


@router.delete("/orgs/{org_id}/risk-rules/{rule_id}", status_code=204)
async def delete_risk_rule(
    session: SessionDep,
    rule_id: UUID,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> Response:
    await build_risk_rule_service(session, ctx.organisation.id, container).delete(rule_id, ctx.user)
    return Response(status_code=204)
