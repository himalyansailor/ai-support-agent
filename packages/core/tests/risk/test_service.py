import asyncio

import pytest
from sqlalchemy import func, select

from supportcore.db.models import AuditLog, Organisation, RiskRule, User
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.risk.defaults import DEFAULT_RULES
from supportcore.risk.service import BuiltinRuleError, InvalidRiskRuleError, RiskRuleService, RuleLockedError


async def _by_name(rules, name) -> RiskRule:
    return next(r for r in await rules.list() if r.name == name)


async def _builtin(rules, key) -> RiskRule:
    return next(r for r in await rules.list() if r.builtin_key == key)


async def test_list_seeds_defaults_in_order(rules):
    listed = await rules.list()
    assert [r.name for r in listed] == [d["name"] for d in DEFAULT_RULES]
    assert [r.position for r in listed] == list(range(8))
    by = {r.builtin_key or r.name: r for r in listed}
    assert by["Refunds"].action == "escalate" and by["Refunds"].keywords == ["refund", "money back", "return for a refund"]
    assert by["low_confidence"].action == "offer_human" and by["repeated_failure"].params == {"failures": 2}
    assert by["write_action"].locked and not by["Refunds"].locked
    assert all(r.enabled for r in listed)
    assert len(await rules.list()) == 8  # second call does not seed again


async def test_list_seeds_defaults_once_when_concurrent(session_factory):
    async with session_factory() as s:
        owner = User(email="o@x.com", password_hash="x", name="O")
        org = Organisation(name="Acme", slug="acme")
        s.add_all([owner, org])
        await s.commit()
        org_id = org.id

    async def first_list():
        async with session_factory() as s:
            result = await RiskRuleService(s, org_id, EventBus()).list()
            await s.commit()
            return len(result)

    counts = await asyncio.gather(*(first_list() for _ in range(4)))
    assert counts == [8, 8, 8, 8]
    async with session_factory() as s:
        total = (await s.execute(select(func.count()).select_from(RiskRule).where(RiskRule.organisation_id == org_id))).scalar_one()
        topics = (await s.execute(select(func.count()).select_from(RiskRule).where(RiskRule.kind == "topic"))).scalar_one()
    assert (total, topics) == (8, 3)


async def test_seeding_is_per_organisation(session, rules, org_b, bus):
    await rules.list()
    assert len(await RiskRuleService(session, org_b.id, bus).list()) == 8


async def test_builtin_cannot_be_deleted_409(rules, owner):
    rule = await _builtin(rules, "frustration")
    with pytest.raises(BuiltinRuleError):
        await rules.delete(rule.id, owner)
    assert len(await rules.list()) == 8


async def test_write_action_locked(rules, owner):
    rule = await _builtin(rules, "write_action")
    with pytest.raises(RuleLockedError):
        await rules.update(rule.id, enabled=False, actor=owner)
    with pytest.raises(RuleLockedError):
        await rules.update(rule.id, action="offer_human", actor=owner)
    with pytest.raises(RuleLockedError):
        await rules.delete(rule.id, owner)
    unchanged = await rules.update(rule.id, enabled=True, action="escalate", actor=owner)  # no-op is fine
    assert unchanged.enabled and unchanged.action == "escalate"


async def test_keywords_validated(rules, owner):
    for bad in (["x"], ["y" * 101], [f"word{i}" for i in range(31)], [""], ["ok", 5]):
        with pytest.raises(InvalidRiskRuleError):
            await rules.create_topic("T", "", bad, "escalate", owner)
    topic = await rules.create_topic("T", "", ["  ab  ", "ab", "cd"], "escalate", owner)
    assert topic.keywords == ["ab", "cd"]  # trimmed and de-duplicated
    ok = await rules.create_topic("Max", "", [f"word{i}" for i in range(30)], "escalate", owner)
    assert len(ok.keywords) == 30
    with pytest.raises(InvalidRiskRuleError):
        await rules.update(topic.id, keywords=["z"], actor=owner)


async def test_name_description_action_validated(rules, owner):
    with pytest.raises(InvalidRiskRuleError):
        await rules.create_topic("  ", "", [], "escalate", owner)
    with pytest.raises(InvalidRiskRuleError):
        await rules.create_topic("x" * 101, "", [], "escalate", owner)
    with pytest.raises(InvalidRiskRuleError):
        await rules.create_topic("T", "d" * 1001, [], "escalate", owner)
    with pytest.raises(InvalidRiskRuleError):
        await rules.create_topic("T", "", [], "explode", owner)


async def test_create_update_delete_topic_with_audit(rules, owner, session, org):
    topic = await rules.create_topic("Legal", "Legal threats", ["lawyer"], "offer_human", owner)
    assert (topic.kind, topic.builtin_key, topic.position, topic.locked) == ("topic", None, 8, False)
    updated = await rules.update(topic.id, name="Legal threats", action="escalate", enabled=False, actor=owner)
    assert (updated.name, updated.action, updated.enabled) == ("Legal threats", "escalate", False)
    await rules.delete(topic.id, owner)
    with pytest.raises(NotFoundError):
        await rules.get(topic.id)
    actions = (
        await session.execute(select(AuditLog.action).where(AuditLog.organisation_id == org.id).order_by(AuditLog.created_at))
    ).scalars()
    assert [a for a in actions if a.startswith("risk_rule.")] == [
        "risk_rule.created", "risk_rule.updated", "risk_rule.deleted",
    ]


async def test_builtin_params_validated_and_name_fixed(rules, owner):
    low = await _builtin(rules, "low_confidence")
    assert (await rules.update(low.id, params={"min_confidence": 0.4}, actor=owner)).params == {"min_confidence": 0.4}
    for bad in ({"min_confidence": 2}, {"min_confidence": "x"}, {"other": 1}):
        with pytest.raises(InvalidRiskRuleError):
            await rules.update(low.id, params=bad, actor=owner)
    rep = await _builtin(rules, "repeated_failure")
    assert (await rules.update(rep.id, params={"failures": 3}, actor=owner)).params == {"failures": 3}
    with pytest.raises(InvalidRiskRuleError):
        await rules.update(rep.id, params={"failures": 0}, actor=owner)
    with pytest.raises(InvalidRiskRuleError):
        await rules.update(rep.id, name="Renamed", actor=owner)
    refunds = await _by_name(rules, "Refunds")
    with pytest.raises(InvalidRiskRuleError):
        await rules.update(refunds.id, params={"failures": 3}, actor=owner)


async def test_reorder(rules, owner):
    listed = await rules.list()
    ids = [r.id for r in listed]
    new_order = [ids[2], ids[0], ids[1]]
    result = await rules.reorder(new_order, owner)
    assert [r.id for r in result][:3] == new_order and [r.id for r in result][3:] == ids[3:]
    assert [r.id for r in await rules.list()][:3] == new_order
    with pytest.raises(InvalidRiskRuleError):
        await rules.reorder([ids[0], ids[0]], owner)
    from uuid import uuid4

    with pytest.raises(NotFoundError):
        await rules.reorder([uuid4()], owner)


async def test_enabled_rules_are_plain_snapshots(rules, owner):
    refunds = await _by_name(rules, "Refunds")
    await rules.update(refunds.id, enabled=False, actor=owner)
    snaps = await rules.enabled_rules()
    assert len(snaps) == 7 and refunds.id not in {s.id for s in snaps}
    cancel = next(s for s in snaps if s.name == "Cancel subscription")
    assert cancel.rule_key == f"topic:{cancel.id}"
    assert next(s for s in snaps if s.builtin_key == "frustration").rule_key == "frustration"
    with pytest.raises(AttributeError):
        cancel.name = "x"  # frozen


async def test_cross_tenant_rule_not_found(session, rules, org_b, bus, owner):
    other = RiskRuleService(session, org_b.id, bus)
    foreign = (await other.list())[0]
    with pytest.raises(NotFoundError):
        await rules.get(foreign.id)
    with pytest.raises(NotFoundError):
        await rules.update(foreign.id, enabled=False, actor=owner)
    with pytest.raises(NotFoundError):
        await rules.delete(foreign.id, owner)
