"""Widgets: public key, allowed domains, appearance, linked knowledge bases and the signed-identity secret.

The identity secret is generated here, encrypted with the SecretCipher and returned in plaintext
exactly once (from `rotate_identity_secret`). Events and audit rows never carry it.
"""

from __future__ import annotations

import re
import secrets
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import KnowledgeBase, User, Widget, WidgetKnowledgeBase
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.secrets.ports import SecretCipher
from supportcore.widget.domains import InvalidDomainError, normalize_domain
from supportcore.widget.events import WidgetCreated, WidgetDeleted, WidgetIdentitySecretRotated, WidgetUpdated

__all__ = [
    "IdentitySecretRequiredError",
    "InvalidAppearanceError",
    "InvalidDomainError",
    "TooManyDomainsError",
    "WidgetService",
]

MAX_NAME_LENGTH = 100
MAX_DOMAINS = 20
MAX_GREETING_LENGTH = 500
MAX_LOGO_URL_LENGTH = 2048
DEFAULT_APPEARANCE: dict[str, Any] = {
    "primary_color": "#4f46e5",
    "position": "bottom-right",
    "launcher": "bubble",
    "logo_url": None,
    "greeting": None,
}
POSITIONS = ("bottom-right", "bottom-left")
LAUNCHERS = ("bubble", "custom")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class InvalidAppearanceError(DomainError):
    pass


class TooManyDomainsError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"A widget can allow at most {MAX_DOMAINS} domains")


class IdentitySecretRequiredError(DomainError):
    def __init__(self) -> None:
        super().__init__("Create an identity secret before requiring signed identity")


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise InvalidAppearanceError(f"Name must be 1-{MAX_NAME_LENGTH} characters")
    return name


def _clean_domains(domains: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(normalize_domain(d) for d in domains))
    if len(cleaned) > MAX_DOMAINS:
        raise TooManyDomainsError()
    return cleaned


def _clean_appearance(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """`changes` merged over `current`; unknown keys and bad values are rejected."""
    unknown = set(changes) - set(DEFAULT_APPEARANCE)
    if unknown:
        raise InvalidAppearanceError(f"Unknown appearance field: {sorted(unknown)[0][:50]}")
    merged = {**DEFAULT_APPEARANCE, **current, **changes}
    color = merged["primary_color"]
    if not isinstance(color, str) or not _COLOR.match(color):
        raise InvalidAppearanceError("primary_color must be a #rrggbb colour")
    merged["primary_color"] = color.lower()
    if merged["position"] not in POSITIONS:
        raise InvalidAppearanceError(f"position must be one of {', '.join(POSITIONS)}")
    if merged["launcher"] not in LAUNCHERS:
        raise InvalidAppearanceError(f"launcher must be one of {', '.join(LAUNCHERS)}")
    logo = merged["logo_url"]
    if logo is not None and not (
        isinstance(logo, str) and logo.startswith("https://") and len(logo) <= MAX_LOGO_URL_LENGTH and logo.strip() == logo
    ):
        raise InvalidAppearanceError("logo_url must be an https URL")
    greeting = merged["greeting"]
    if greeting is not None:
        if not isinstance(greeting, str) or len(greeting.strip()) > MAX_GREETING_LENGTH:
            raise InvalidAppearanceError(f"greeting must be at most {MAX_GREETING_LENGTH} characters")
        merged["greeting"] = greeting.strip() or None
    return merged


class WidgetService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, events: EventBus, cipher: SecretCipher) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._cipher = cipher
        self._widgets = TenantScopedRepository(session, Widget, organisation_id)
        self._kbs = TenantScopedRepository(session, KnowledgeBase, organisation_id)

    async def list(self) -> list[Widget]:
        return await self._widgets.list(order_by=(Widget.created_at, Widget.id), limit=1000)

    async def get(self, widget_id: UUID, *, for_update: bool = False) -> Widget:
        stmt = self._widgets.select(for_update=for_update).where(Widget.id == widget_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)
        widget = (await self._session.execute(stmt)).scalar_one_or_none()
        if widget is None:
            raise NotFoundError("Widget not found")
        return widget

    @staticmethod
    async def get_by_public_key(session: AsyncSession, key: str) -> Widget | None:
        """The enabled widget with this public key, or None. Deliberately not tenant-scoped: the key
        is what identifies the organisation for the public widget API."""
        stmt = select(Widget).where(Widget.public_key == key, Widget.enabled.is_(True))
        return (await session.execute(stmt)).scalar_one_or_none()

    async def knowledge_base_ids(self, widget_id: UUID) -> list[UUID]:
        await self.get(widget_id)  # 404 for a foreign widget
        stmt = (
            select(WidgetKnowledgeBase.knowledge_base_id)
            .where(WidgetKnowledgeBase.widget_id == widget_id)
            .order_by(WidgetKnowledgeBase.knowledge_base_id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def create(self, name: str, actor: User) -> Widget:
        widget = Widget(
            name=_clean_name(name),
            public_key=await self._new_public_key(),
            appearance=dict(DEFAULT_APPEARANCE),
            allowed_domains=[],
            created_by_user_id=actor.id,
        )
        await self._widgets.add(widget)
        await self._session.flush()
        await self._events.publish(
            WidgetCreated(organisation_id=self._org_id, actor_user_id=actor.id, widget_id=widget.id, name=widget.name)
        )
        return widget

    async def update(
        self,
        widget_id: UUID,
        *,
        name: str | None = None,
        enabled: bool | None = None,
        allowed_domains: list[str] | None = None,
        appearance: dict[str, Any] | None = None,
        csat_enabled: bool | None = None,
        identity_required: bool | None = None,
        knowledge_base_ids: list[UUID] | None = None,
        actor: User,
    ) -> Widget:
        """None means unchanged. `appearance` is merged over the current one; `knowledge_base_ids` replaces the links."""
        widget = await self.get(widget_id, for_update=True)
        changes: dict[str, Any] = {}

        def change(field: str, new: Any) -> None:
            old = getattr(widget, field)
            if new != old:
                changes[field] = {"from": old, "to": new}
                setattr(widget, field, new)

        if name is not None:
            change("name", _clean_name(name))
        if enabled is not None:
            change("enabled", enabled)
        if allowed_domains is not None:
            change("allowed_domains", _clean_domains(allowed_domains))
        if appearance is not None:
            change("appearance", _clean_appearance(widget.appearance, appearance))
        if csat_enabled is not None:
            change("csat_enabled", csat_enabled)
        if identity_required is not None:
            if identity_required and not widget.encrypted_identity_secret:
                raise IdentitySecretRequiredError()
            change("identity_required", identity_required)
        if knowledge_base_ids is not None:
            old_ids = await self.knowledge_base_ids(widget.id)
            new_ids = await self._check_kbs(list(dict.fromkeys(knowledge_base_ids)))
            if set(new_ids) != set(old_ids):
                await self._session.execute(delete(WidgetKnowledgeBase).where(WidgetKnowledgeBase.widget_id == widget.id))
                self._session.add_all([WidgetKnowledgeBase(widget_id=widget.id, knowledge_base_id=k) for k in new_ids])
                changes["knowledge_base_ids"] = {"from": [str(k) for k in old_ids], "to": [str(k) for k in new_ids]}
        if changes:
            await self._session.flush()
            await self._events.publish(
                WidgetUpdated(
                    organisation_id=self._org_id,
                    actor_user_id=actor.id,
                    widget_id=widget.id,
                    name=widget.name,
                    changes=changes,
                )
            )
        return widget

    async def delete(self, widget_id: UUID, actor: User) -> None:
        widget = await self.get(widget_id, for_update=True)
        event = WidgetDeleted(
            organisation_id=self._org_id, actor_user_id=actor.id, widget_id=widget.id, name=widget.name
        )
        await self._widgets.delete(widget)  # widget_knowledge_bases rows cascade
        await self._events.publish(event)

    async def rotate_identity_secret(self, widget_id: UUID, actor: User) -> tuple[Widget, str]:
        """Replaces the secret; the plaintext is returned here and never again."""
        widget = await self.get(widget_id, for_update=True)
        secret = "wsec_" + secrets.token_urlsafe(32)
        widget.encrypted_identity_secret = self._cipher.encrypt(secret)
        widget.identity_secret_last4 = secret[-4:]
        await self._session.flush()
        await self._events.publish(
            WidgetIdentitySecretRotated(
                organisation_id=self._org_id, actor_user_id=actor.id, widget_id=widget.id, name=widget.name
            )
        )
        return widget, secret

    # --- internals ---------------------------------------------------------------------------------

    async def _new_public_key(self) -> str:
        while True:
            key = "wk_" + secrets.token_urlsafe(18)  # 24 urlsafe characters
            taken = (await self._session.execute(select(Widget.id).where(Widget.public_key == key))).first()
            if taken is None:
                return key

    async def _check_kbs(self, kb_ids: list[UUID]) -> list[UUID]:
        """Every id must be a knowledge base of this organisation, else NotFoundError."""
        if not kb_ids:
            return []
        stmt = self._kbs.select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids))
        found = set((await self._session.execute(stmt)).scalars())
        if found != set(kb_ids):
            raise NotFoundError("Knowledge base not found")
        return kb_ids
