"""Risk rules: the defaults seeded on first use, topic CRUD, ordering and plain snapshots for the bot."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import RiskRule, User
from supportcore.db.models.risk import RISK_ACTIONS
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.risk.defaults import DEFAULT_RULES
from supportcore.risk.events import RiskRuleCreated, RiskRuleDeleted, RiskRuleUpdated
from supportcore.risk.ports import RiskRuleSnapshot

__all__ = ["BuiltinRuleError", "InvalidRiskRuleError", "RiskRuleService", "RuleLockedError", "snapshot"]

MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 1000
MAX_KEYWORDS = 30
MIN_KEYWORD_LENGTH = 2
MAX_KEYWORD_LENGTH = 100
MAX_FAILURES = 20


class InvalidRiskRuleError(DomainError):
    pass


class BuiltinRuleError(DomainError):
    def __init__(self) -> None:
        super().__init__("Built-in rules cannot be deleted; disable them instead")


class RuleLockedError(DomainError):
    def __init__(self) -> None:
        super().__init__("This rule is locked and cannot be changed")


def snapshot(rule: RiskRule) -> RiskRuleSnapshot:
    return RiskRuleSnapshot(
        id=rule.id,
        kind=rule.kind,
        builtin_key=rule.builtin_key,
        name=rule.name,
        description=rule.description,
        keywords=list(rule.keywords),
        action=rule.action,
        enabled=rule.enabled,
        locked=rule.locked,
        position=rule.position,
        params=dict(rule.params),
    )


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise InvalidRiskRuleError(f"Name must be 1-{MAX_NAME_LENGTH} characters")
    return name


def _clean_description(description: str) -> str:
    description = (description or "").strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise InvalidRiskRuleError(f"Description must be at most {MAX_DESCRIPTION_LENGTH} characters")
    return description


def _clean_keywords(keywords: Sequence[str]) -> list[str]:
    if not isinstance(keywords, (list, tuple)) or not all(isinstance(k, str) for k in keywords):
        raise InvalidRiskRuleError("Keywords must be a list of strings")
    cleaned = list(dict.fromkeys(k.strip() for k in keywords))
    if len(cleaned) > MAX_KEYWORDS:
        raise InvalidRiskRuleError(f"A rule can have at most {MAX_KEYWORDS} keywords")
    if any(not MIN_KEYWORD_LENGTH <= len(k) <= MAX_KEYWORD_LENGTH for k in cleaned):
        raise InvalidRiskRuleError(f"Each keyword must be {MIN_KEYWORD_LENGTH}-{MAX_KEYWORD_LENGTH} characters")
    return cleaned


def _clean_action(action: str) -> str:
    if action not in RISK_ACTIONS:
        raise InvalidRiskRuleError(f"Action must be one of {', '.join(RISK_ACTIONS)}")
    return action


def _clean_params(rule: RiskRule, params: dict[str, Any]) -> dict[str, Any]:
    """Only low_confidence (min_confidence, 0-1) and repeated_failure (failures, 1-20) take params."""
    if not isinstance(params, dict):
        raise InvalidRiskRuleError("params must be an object")
    allowed = {"low_confidence": "min_confidence", "repeated_failure": "failures"}.get(rule.builtin_key or "")
    unknown = set(params) - ({allowed} if allowed else set())
    if unknown:
        raise InvalidRiskRuleError(f"Unknown param: {sorted(unknown)[0][:50]}")
    merged = {**rule.params, **params}
    if allowed == "min_confidence":
        value = merged["min_confidence"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise InvalidRiskRuleError("min_confidence must be a number from 0 to 1")
        merged["min_confidence"] = float(value)
    elif allowed == "failures":
        value = merged["failures"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_FAILURES:
            raise InvalidRiskRuleError(f"failures must be a whole number from 1 to {MAX_FAILURES}")
    return merged


class RiskRuleService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, events: EventBus) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._rules = TenantScopedRepository(session, RiskRule, organisation_id)

    async def list(self) -> list[RiskRule]:
        await self._ensure_seeded()
        return await self._rules.list(order_by=(RiskRule.position, RiskRule.id), limit=1000)

    async def enabled_rules(self) -> list[RiskRuleSnapshot]:
        """Plain snapshots of the enabled rules, in position order; safe to use after the session closes."""
        return [snapshot(r) for r in await self.list() if r.enabled]

    async def get(self, rule_id: UUID, *, for_update: bool = False) -> RiskRule:
        stmt = self._rules.select(for_update=for_update).where(RiskRule.id == rule_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)
        rule = (await self._session.execute(stmt)).scalar_one_or_none()
        if rule is None:
            raise NotFoundError("Risk rule not found")
        return rule

    async def create_topic(
        self, name: str, description: str, keywords: Sequence[str], action: str, actor: User
    ) -> RiskRule:
        await self._ensure_seeded()
        top = (await self._session.execute(self._rules.select(func.max(RiskRule.position)))).scalar_one()
        rule = RiskRule(
            kind="topic",
            name=_clean_name(name),
            description=_clean_description(description),
            keywords=_clean_keywords(keywords),
            action=_clean_action(action),
            enabled=True,
            locked=False,
            position=0 if top is None else top + 1,
            params={},
        )
        await self._rules.add(rule)
        await self._session.flush()
        await self._events.publish(
            RiskRuleCreated(
                organisation_id=self._org_id,
                actor_user_id=actor.id,
                rule_id=rule.id,
                name=rule.name,
                kind=rule.kind,
                action=rule.action,
            )
        )
        return rule

    async def update(
        self,
        rule_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        keywords: Sequence[str] | None = None,
        action: str | None = None,
        enabled: bool | None = None,
        params: dict[str, Any] | None = None,
        actor: User,
    ) -> RiskRule:
        """None means unchanged. A locked rule rejects any real change; built-ins keep their name and keywords."""
        rule = await self.get(rule_id, for_update=True)
        changes: dict[str, Any] = {}
        new_values: dict[str, Any] = {}
        if name is not None:
            new_values["name"] = _clean_name(name)
        if description is not None:
            new_values["description"] = _clean_description(description)
        if keywords is not None:
            new_values["keywords"] = _clean_keywords(keywords)
        if action is not None:
            new_values["action"] = _clean_action(action)
        if enabled is not None:
            new_values["enabled"] = enabled
        if params is not None:
            new_values["params"] = _clean_params(rule, params)
        for field_name, new in new_values.items():
            old = getattr(rule, field_name)
            if new != old:
                changes[field_name] = {"from": old, "to": new}
        if changes and rule.locked:
            raise RuleLockedError()
        if rule.kind == "builtin" and ({"name", "keywords"} & set(changes)):
            raise InvalidRiskRuleError("A built-in rule's name and keywords cannot be changed")
        if changes:
            for field_name, diff in changes.items():
                setattr(rule, field_name, diff["to"])
            await self._session.flush()
            await self._publish_updated(rule, actor, changes)
        return rule

    async def delete(self, rule_id: UUID, actor: User) -> None:
        rule = await self.get(rule_id, for_update=True)
        if rule.locked:
            raise RuleLockedError()
        if rule.kind == "builtin":
            raise BuiltinRuleError()
        event = RiskRuleDeleted(
            organisation_id=self._org_id, actor_user_id=actor.id, rule_id=rule.id, name=rule.name, kind=rule.kind
        )
        await self._rules.delete(rule)
        await self._events.publish(event)

    async def reorder(self, ids: Sequence[UUID], actor: User) -> list[RiskRule]:
        """The listed rules take positions 0..n-1 in that order; unlisted ones follow in their current order."""
        ids = list(ids)
        if len(set(ids)) != len(ids):
            raise InvalidRiskRuleError("Each rule can be listed once")
        rules = await self.list()
        by_id = {r.id: r for r in rules}
        if any(i not in by_id for i in ids):
            raise NotFoundError("Risk rule not found")
        ordered = [by_id[i] for i in ids] + [r for r in rules if r.id not in set(ids)]
        moved: list[tuple[RiskRule, int, int]] = []
        for position, rule in enumerate(ordered):
            if rule.position != position:
                moved.append((rule, rule.position, position))
                rule.position = position
        if moved:
            await self._session.flush()
            for rule, old, new in moved:
                await self._publish_updated(rule, actor, {"position": {"from": old, "to": new}})
        return ordered

    # --- internals ---------------------------------------------------------------------------------

    async def _publish_updated(self, rule: RiskRule, actor: User, changes: dict[str, Any]) -> None:
        await self._events.publish(
            RiskRuleUpdated(
                organisation_id=self._org_id,
                actor_user_id=actor.id,
                rule_id=rule.id,
                name=rule.name,
                kind=rule.kind,
                changes=changes,
            )
        )

    async def _ensure_seeded(self) -> None:
        """Inserts the defaults when the organisation has no rules. A per-organisation advisory lock
        (held to the end of the transaction) makes concurrent first calls queue up, so the topics
        (which have no unique key) are never inserted twice; built-ins are also ON CONFLICT DO NOTHING.
        Built-ins can never be deleted, so a non-empty table means "already seeded"."""
        exists = self._rules.select(RiskRule.id).limit(1)
        if (await self._session.execute(exists)).first() is not None:
            return
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"risk_rules_seed:{self._org_id}"}
        )
        if (await self._session.execute(exists)).first() is not None:  # a concurrent call seeded meanwhile
            return
        rows = [
            {
                "organisation_id": self._org_id,
                "kind": d["kind"],
                "builtin_key": d.get("builtin_key"),
                "name": d["name"],
                "description": d["description"],
                "keywords": d.get("keywords", []),
                "action": d["action"],
                "enabled": True,
                "locked": d.get("locked", False),
                "position": position,
                "params": d.get("params", {}),
            }
            for position, d in enumerate(DEFAULT_RULES)
        ]
        await self._session.execute(
            pg_insert(RiskRule)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["organisation_id", "builtin_key"])
        )
