"""The public side of a widget: its config, the frame policy and starting a customer session.

Everything here is reachable without dashboard auth, so the widget's public key is the only input that
selects a tenant. Origins are checked against the widget's allowed domains before anything is created.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.config import Settings
from supportcore.db.models import Conversation, Customer, Organisation, Widget
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.observability.context import bind_organisation
from supportcore.voice.browser_calls import browser_calls_available
from supportcore.widget.domains import frame_ancestors, origin_allowed
from supportcore.widget.identity.ports import InvalidCustomerTokenError, VerifiedCustomer
from supportcore.widget.service import WidgetService
from supportcore.widget.strings import strings_for
from supportcore.widget.tokens import WidgetTokenService, widget_tokens_from_settings

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = [
    "IdentityRequiredError",
    "OriginNotAllowedError",
    "PublicWidgetConfig",
    "SessionStart",
    "WidgetSessionService",
]

FRAME_ANCESTORS_NONE = "'none'"
_NO_ORIGIN = "\0"  # origin_allowed() always accepts web_origin; here nothing is special
_OPEN_STATUSES = ("bot", "open", "pending")


class OriginNotAllowedError(DomainError):
    def __init__(self) -> None:
        super().__init__("This site is not allowed to embed the widget")


class IdentityRequiredError(DomainError):
    def __init__(self) -> None:
        super().__init__("This widget requires a signed customer identity")


@dataclass(frozen=True)
class PublicWidgetConfig:
    appearance: dict[str, Any]
    strings: dict[str, str]
    language: str
    csat_enabled: bool
    organisation_name: str
    voice_call_enabled: bool = False  # browser calling is ready for this widget


@dataclass(frozen=True)
class SessionStart:
    token: str
    customer_id: UUID
    visitor_id: str
    # The customer's most recent open conversation (bot or waiting for a person), to resume.
    conversation: Conversation | None


def _new_visitor_id() -> str:
    return "v_" + secrets.token_urlsafe(18)


class WidgetSessionService:
    def __init__(self, session: AsyncSession, adapters: "Adapters", settings: Settings, events: EventBus) -> None:
        self._session = session
        self._adapters = adapters
        self._settings = settings
        self._events = events
        self.tokens: WidgetTokenService = widget_tokens_from_settings(settings)

    async def public_config(self, public_key: str, *, preview: bool = False) -> PublicWidgetConfig:
        """`preview`: the dashboard preview, which cannot place calls."""
        widget = await self._widget(public_key)
        organisation = await self._organisation(widget)
        strings = strings_for(organisation.language)
        greeting = widget.appearance.get("greeting")
        if greeting:
            strings["greeting"] = greeting
        return PublicWidgetConfig(
            appearance=dict(widget.appearance),
            strings=strings,
            language=organisation.language,
            csat_enabled=widget.csat_enabled,
            organisation_name=organisation.name,
            voice_call_enabled=not preview
            and await browser_calls_available(
                self._session, widget.organisation_id, widget.id, self._settings, self._events
            ),
        )

    async def frame_policy(self, public_key: str) -> str:
        """The `frame-ancestors` sources for the iframe page: the sites allowed to embed it."""
        widget = await WidgetService.get_by_public_key(self._session, public_key)
        if widget is None:
            return FRAME_ANCESTORS_NONE
        directive = frame_ancestors(widget.allowed_domains, self._settings.web_origin)
        return directive.removeprefix("frame-ancestors ")

    async def start(
        self,
        public_key: str,
        *,
        parent_origin: str,
        visitor_id: str | None,
        customer_token: str | None,
        is_preview: bool,
        preview_ticket: str | None = None,
    ) -> SessionStart:
        widget = await self._widget(public_key)
        if is_preview:
            # The dashboard preview needs its origin AND a ticket a signed-in user got for this widget.
            if parent_origin != self._settings.web_origin or not self.tokens.preview_ticket_valid(
                preview_ticket, widget.organisation_id, widget.id
            ):
                raise OriginNotAllowedError()
        elif not parent_origin or not origin_allowed(parent_origin, widget.allowed_domains, web_origin=_NO_ORIGIN):
            # The dashboard origin is not an embedding site: with no allowed domains only previews work.
            raise OriginNotAllowedError()
        preview = is_preview

        verified = self._verify_identity(widget, customer_token)
        customers = TenantScopedRepository(self._session, Customer, widget.organisation_id)
        if verified is not None:
            customer = await self._identified_customer(customers, verified, visitor_id)
        else:
            customer = await self._anonymous_customer(customers, visitor_id)
        if customer.visitor_id is None:
            customer.visitor_id = _new_visitor_id()
        customer.last_seen_at = datetime.now(UTC)
        await self._session.flush()

        conversation = await self._open_conversation(widget.organisation_id, customer.id)
        token = self.tokens.issue(customer.id, widget.organisation_id, widget.id, preview=preview)
        return SessionStart(
            token=token,
            customer_id=customer.id,
            visitor_id=customer.visitor_id,
            conversation=conversation,
        )

    # --- internals ---------------------------------------------------------------------------------

    async def _widget(self, public_key: str) -> Widget:
        widget = await WidgetService.get_by_public_key(self._session, public_key)
        if widget is None:
            raise NotFoundError("Widget not found")
        bind_organisation(widget.organisation_id)
        return widget

    async def _organisation(self, widget: Widget) -> Organisation:
        organisation = await self._session.get(Organisation, widget.organisation_id)
        assert organisation is not None  # organisation_id is a cascading foreign key
        return organisation

    def _verify_identity(self, widget: Widget, customer_token: str | None) -> VerifiedCustomer | None:
        if not customer_token:
            if widget.identity_required:
                raise IdentityRequiredError()
            return None
        if not widget.encrypted_identity_secret:
            raise InvalidCustomerTokenError()
        secret = self._adapters.cipher.decrypt(widget.encrypted_identity_secret)
        return self._adapters.customer_token_verifier.verify(customer_token, secret)

    async def _identified_customer(
        self, customers: TenantScopedRepository[Customer], verified: VerifiedCustomer, visitor_id: str | None
    ) -> Customer:
        """The customer with this external id; the visitor's anonymous customer becomes it if none exists yet."""
        stmt = customers.select().where(Customer.external_id == verified.external_id)
        customer = (await self._session.execute(stmt)).scalar_one_or_none()
        if customer is None and visitor_id:
            customer = await self._by_visitor_id(customers, visitor_id)  # anonymous only
        if customer is None:
            candidate = Customer(external_id=verified.external_id)
            try:
                async with self._session.begin_nested():
                    await customers.add(candidate)
                    await self._session.flush()
                customer = candidate
            except IntegrityError:  # a concurrent first session for the same user won the unique index
                customer = (await self._session.execute(stmt)).scalar_one_or_none()
                if customer is None:
                    raise
        customer.external_id = verified.external_id
        if verified.email:
            customer.email = verified.email
        if verified.name:
            customer.name = verified.name
        return customer

    async def _anonymous_customer(self, customers: TenantScopedRepository[Customer], visitor_id: str | None) -> Customer:
        customer = await self._by_visitor_id(customers, visitor_id) if visitor_id else None
        if customer is None:
            # Never adopt a client-chosen id: an unknown one gets a fresh random id.
            customer = Customer(visitor_id=_new_visitor_id())
            await customers.add(customer)
        return customer

    async def _by_visitor_id(self, customers: TenantScopedRepository[Customer], visitor_id: str) -> Customer | None:
        # An identified customer is not reachable by visitor id alone.
        stmt = customers.select().where(Customer.visitor_id == visitor_id, Customer.external_id.is_(None))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _open_conversation(self, organisation_id: UUID, customer_id: UUID) -> Conversation | None:
        repo = TenantScopedRepository(self._session, Conversation, organisation_id)
        stmt = (
            repo.select()
            .where(Conversation.customer_id == customer_id, Conversation.status.in_(_OPEN_STATUSES))
            .order_by(Conversation.last_message_at.desc(), Conversation.id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
