"""The organisation's Twilio account and phone numbers.

Methods that call Twilio read what they need, commit, call Twilio with no transaction open, and only then
write (autobegin opens the next unit of work on the same session). Decrypted tokens exist only inside a
`TelephonyAccountRef` handed to one adapter call. Nothing here logs a token, a key or a phone number.
"""

import asyncio
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.analytics.integration_errors import record_integration_error
from supportcore.billing.entitlements import EntitlementService
from supportcore.config import Settings
from supportcore.db.models import PhoneNumber, TelephonyAccount, User, Widget
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.secrets.ports import SecretCipher, SecretDecryptError
from supportcore.voice.errors import (
    InvalidVoiceSettingsError,
    PhoneNumberUnavailableError,
    TelephonyAccountInUseError,
    TelephonyCredentialsError,
    TelephonyNotConfiguredError,
    TelephonyUnavailableError,
)
from supportcore.voice.events import (
    PhoneNumberAdded,
    PhoneNumberReleased,
    TelephonyConnected,
    TelephonyDisconnected,
)
from supportcore.voice.telephony.ports import (
    AvailableNumber,
    BrowserCalling,
    NumberWebhooks,
    OwnedNumber,
    TelephonyAccountRef,
    TelephonyAuthError,
    TelephonyError,
)

__all__ = ["AccountNumber", "TelephonyService"]

logger = structlog.get_logger()

_ACCOUNT_SID = re.compile(r"^AC[0-9a-f]{32}$")
_E164 = re.compile(r"^\+[1-9][0-9]{6,14}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_AREA_CODE = re.compile(r"^[0-9]{1,10}$")
_TOKEN_LENGTH = 32
_UNSET: Any = object()


@dataclass(frozen=True)
class AccountNumber:
    provider_sid: str
    e164: str
    connected: bool


class TelephonyService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        adapters: Any,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._adapters = adapters
        self._settings = settings
        self._session_factory = session_factory
        self._accounts = TenantScopedRepository(session, TelephonyAccount, organisation_id)
        self._numbers = TenantScopedRepository(session, PhoneNumber, organisation_id)

    # --- reads ------------------------------------------------------------------------------------

    async def get_account(self) -> TelephonyAccount | None:
        return (await self._session.execute(self._accounts.select())).scalar_one_or_none()

    async def list_numbers(self) -> list[PhoneNumber]:
        return await self._numbers.list(order_by=(PhoneNumber.created_at, PhoneNumber.id), limit=1000)

    def platform_available(self) -> bool:
        return bool(self._settings.twilio_account_sid and self._settings.twilio_auth_token)

    def inbound_url(self) -> str | None:
        base = self._settings.voice_public_url
        return f"{base}/twilio/voice/inbound" if base else None

    def webhooks_out_of_date(self, number: PhoneNumber) -> bool:
        current = self.inbound_url()
        return current is not None and number.webhook_url != current

    # --- account ----------------------------------------------------------------------------------

    async def connect_byo(self, account_sid: str, auth_token: str, actor: User) -> TelephonyAccount:
        """Verifies the credentials, sets up browser calling and stores them encrypted. Nothing is stored when
        Twilio rejects them. Saving new credentials resets an `auth_failed` account."""
        if not isinstance(account_sid, str) or not _ACCOUNT_SID.match(account_sid):
            raise TelephonyCredentialsError("The account SID must look like AC followed by 32 hex characters")
        if not isinstance(auth_token, str) or len(auth_token) != _TOKEN_LENGTH:
            raise TelephonyCredentialsError(f"The auth token must be {_TOKEN_LENGTH} characters")
        browser_hooks = self._browser_webhooks()
        existing = await self.get_account()
        if existing is not None:
            if existing.kind == "platform" and await self.list_numbers():
                raise TelephonyAccountInUseError("Release the numbers bought through the platform first")
            if existing.account_sid != account_sid and await self.list_numbers():
                raise TelephonyAccountInUseError("Disconnect the current account before connecting another")
        # Unscoped by design: an account SID is a public identifier and belongs to at most one organisation.
        taken = select(TelephonyAccount.id).where(
            TelephonyAccount.account_sid == account_sid, TelephonyAccount.organisation_id != self._org_id
        )
        if (await self._session.execute(taken)).first() is not None:
            raise TelephonyAccountInUseError("This Twilio account is already connected to another organisation")
        replaced_platform_sid = existing.account_sid if existing and existing.kind == "platform" else None
        previous = self._browser_calling(existing) if existing and existing.account_sid == account_sid else None
        ref = TelephonyAccountRef(account_sid=account_sid, auth_token=auth_token)
        await self._session.commit()
        try:
            await self._adapters.telephony.verify(ref)
            calling = await self._adapters.telephony.ensure_browser_calling(ref, browser_hooks, previous)
        except TelephonyAuthError:
            raise TelephonyCredentialsError("Twilio rejected these credentials") from None
        except TelephonyError as exc:
            await self._provider_failed(exc)
        values = self._account_values(auth_token, calling)
        try:
            async with self._session.begin_nested():
                account = await self._upsert_account(existing, kind="byo", account_sid=account_sid, **values)
        except IntegrityError:
            raise TelephonyAccountInUseError("This Twilio account is already connected to another organisation") from None
        await self._events.publish(
            TelephonyConnected(organisation_id=self._org_id, actor_user_id=actor.id, account_id=account.id, kind="byo")
        )
        if replaced_platform_sid is not None and replaced_platform_sid != account_sid:
            await self._session.commit()
            await self._close_subaccount(replaced_platform_sid)
        return account

    async def disconnect(self, actor: User) -> None:
        """BYO only: takes our webhooks off every number Twilio still points at us, then forgets the account."""
        account = await self.get_account()
        if account is None:
            raise NotFoundError("Telephony account not found")
        if account.kind != "byo":
            raise TelephonyNotConfiguredError("This account belongs to the platform; release its numbers instead")
        ref = self._ref(account)
        ours = {n.webhook_url for n in await self.list_numbers() if n.webhook_url}
        if (url := self.inbound_url()) is not None:
            ours.add(url)
        await self._session.commit()
        try:
            for owned in await self._adapters.telephony.list_numbers(ref):
                if owned.voice_url in ours:
                    await self._adapters.telephony.configure_number(ref, owned.provider_sid, None)
        except (TelephonyAuthError, TelephonyError):
            # A revoked token or an unreachable Twilio must not keep the account connected here.
            logger.warning("telephony_webhook_release_skipped", organisation_id=str(self._org_id))
        account = await self.get_account()
        if account is None:
            raise NotFoundError("Telephony account not found")
        for number in await self.list_numbers():
            await self._numbers.delete(number)
        event = TelephonyDisconnected(organisation_id=self._org_id, actor_user_id=actor.id, account_id=account.id)
        await self._accounts.delete(account)
        await self._events.publish(event)

    # --- searching, buying, connecting ------------------------------------------------------------

    async def search(self, country: str, area_code: str | None) -> list[AvailableNumber]:
        if not isinstance(country, str) or not _COUNTRY.match(country):
            raise InvalidVoiceSettingsError("Country must be a two-letter code")
        if area_code is not None and not _AREA_CODE.match(area_code):
            raise InvalidVoiceSettingsError("Area code must be digits")
        account = await self.get_account()
        if account is not None:
            ref, stored = self._ref(account), account
        else:
            ref, stored = self._master(), None
        await self._session.commit()
        return await self._call(self._adapters.telephony.search_numbers(ref, country, area_code), stored)

    async def buy(self, e164: str, widget_id: UUID | None, actor: User) -> PhoneNumber:
        if not self.platform_available():
            raise TelephonyNotConfiguredError("Buying numbers needs platform Twilio credentials")
        if not isinstance(e164, str) or not _E164.match(e164):
            raise InvalidVoiceSettingsError("The number is not a valid E.164 number")
        webhooks = self._webhooks()
        await self._check_widget(widget_id)
        await self._check_phone_lines()
        account = await self.get_account()
        if account is not None and account.kind != "platform":
            raise TelephonyNotConfiguredError("Numbers are bought on platform accounts only; connect an existing one")
        if account is None:
            account = await self._create_platform_account(actor)
        ref = self._ref(account)
        if self._browser_calling(account) is None:  # a purchase retried after browser calling setup failed
            await self._session.commit()
            calling = await self._call(
                self._adapters.telephony.ensure_browser_calling(ref, self._browser_webhooks(), None), account
            )
            self._store_calling(account, calling)
        await self._session.commit()
        owned = await self._call(self._adapters.telephony.buy_number(ref, e164, webhooks), account)
        try:
            number = await self._add_number(account, owned, "platform", widget_id, webhooks.voice_url, actor)
            await self._session.commit()  # the purchase is recorded here, not by the request's final commit
        except BaseException:
            await asyncio.shield(self._undo_purchase(ref, owned.provider_sid))
            raise
        return number

    async def list_account_numbers(self) -> list[AccountNumber]:
        """BYO: the numbers on the organisation's own Twilio account, and whether each is connected here."""
        account = await self._byo_account()
        ref = self._ref(account)
        await self._session.commit()
        owned = await self._call(self._adapters.telephony.list_numbers(ref), account)
        connected = {n.provider_sid for n in await self.list_numbers()}
        return [AccountNumber(o.provider_sid, o.e164, o.provider_sid in connected) for o in owned]

    async def connect_number(self, provider_sid: str, widget_id: UUID | None, actor: User) -> PhoneNumber:
        account = await self._byo_account()
        webhooks = self._webhooks()
        await self._check_widget(widget_id)
        if any(n.provider_sid == provider_sid for n in await self.list_numbers()):
            raise PhoneNumberUnavailableError("This number is already connected")
        await self._check_phone_lines()
        ref = self._ref(account)
        await self._session.commit()
        owned = next(
            (o for o in await self._call(self._adapters.telephony.list_numbers(ref), account) if o.provider_sid == provider_sid),
            None,
        )
        if owned is None:
            raise NotFoundError("Number not found on the Twilio account")
        # Unscoped by design: an E.164 number is a public identifier and belongs to at most one organisation.
        if (await self._session.execute(select(PhoneNumber.id).where(PhoneNumber.e164 == owned.e164))).first():
            raise PhoneNumberUnavailableError("This number is already connected")
        await self._session.commit()
        await self._call(self._adapters.telephony.configure_number(ref, provider_sid, webhooks), account)
        return await self._add_number(account, owned, "byo", widget_id, webhooks.voice_url, actor)

    async def update_number(
        self, number_id: UUID, *, label: Any = _UNSET, widget_id: Any = _UNSET, enabled: Any = _UNSET
    ) -> PhoneNumber:
        number = await self._number(number_id, for_update=True)
        if label is not _UNSET:
            if label is not None and (not isinstance(label, str) or len(label) > 100):
                raise InvalidVoiceSettingsError("Label must be at most 100 characters")
            number.label = label.strip() or None if label is not None else None
        if widget_id is not _UNSET:
            await self._check_widget(widget_id)
            number.widget_id = widget_id
        if enabled is not _UNSET:
            if not isinstance(enabled, bool):
                raise InvalidVoiceSettingsError("enabled must be true or false")
            number.enabled = enabled
        await self._session.flush()
        return number

    async def release(self, number_id: UUID, actor: User) -> None:
        """Platform numbers are released at Twilio. BYO numbers stay on the customer's account; our webhooks are
        taken off only when Twilio still points at us."""
        number = await self._number(number_id)
        account = await self._account_for(number)
        ref = self._ref(account)
        sid, ownership, ours = number.provider_sid, number.ownership, {number.webhook_url, self.inbound_url()}
        await self._session.commit()
        if ownership == "platform":
            await self._call(self._adapters.telephony.release_number(ref, sid), account, tolerate_missing=True)
        else:
            owned = await self._call(self._adapters.telephony.list_numbers(ref), account)
            current = next((o for o in owned if o.provider_sid == sid), None)
            if current is not None and current.voice_url is not None and current.voice_url in ours:
                await self._call(self._adapters.telephony.configure_number(ref, sid, None), account)
        number = await self._number(number_id)
        event = PhoneNumberReleased(
            organisation_id=self._org_id, actor_user_id=actor.id, number_id=number.id, ownership=ownership
        )
        await self._numbers.delete(number)
        await self._events.publish(event)

    async def sync_webhooks(self) -> int:
        """Re-points every number whose stored webhook URL is not the current one, and refreshes the TwiML App's
        voice URL and status callback. Returns how many numbers changed."""
        webhooks = self._webhooks()
        browser_hooks = self._browser_webhooks()
        account = await self.get_account()
        if account is None:
            return 0
        stale = [(n.id, n.provider_sid) for n in await self.list_numbers() if n.webhook_url != webhooks.voice_url]
        ref = self._ref(account)
        existing = self._browser_calling(account)
        await self._session.commit()
        done: list[UUID] = []
        calling: BrowserCalling | None = None
        failure: Exception | None = None
        try:
            for number_id, sid in stale:
                await self._call(self._adapters.telephony.configure_number(ref, sid, webhooks), account)
                done.append(number_id)
            if existing is not None:
                calling = await self._call(
                    self._adapters.telephony.ensure_browser_calling(ref, browser_hooks, existing), account
                )
        except Exception as exc:  # noqa: BLE001  save what was done, then report the failure
            failure = exc
        for number_id in done:
            number = await self._numbers.get(number_id)
            if number is not None:
                number.webhook_url = webhooks.voice_url
        if calling is not None and calling != existing:
            account = await self.get_account()
            if account is not None:
                self._store_calling(account, calling)
        if failure is not None:
            await self._session.commit()
            raise failure
        await self._session.flush()
        return len(done)

    # --- internals --------------------------------------------------------------------------------

    def _browser_webhooks(self) -> NumberWebhooks:
        """The TwiML App's URLs: calls placed through it report their status only to its status callback."""
        return NumberWebhooks(voice_url=self._public("browser"), status_callback=self._public("status"))

    def _public(self, leaf: str) -> str:
        base = self._settings.voice_public_url
        if not base:
            raise TelephonyNotConfiguredError("SUPPORT_VOICE_PUBLIC_URL is not set")
        return f"{base}/twilio/voice/{leaf}"

    def _webhooks(self) -> NumberWebhooks:
        return NumberWebhooks(voice_url=self._public("inbound"), status_callback=self._public("status"))

    @property
    def _cipher(self) -> SecretCipher:
        return self._adapters.cipher

    def _master(self) -> TelephonyAccountRef:
        if not self.platform_available():
            raise TelephonyNotConfiguredError("Platform Twilio credentials are not configured")
        return TelephonyAccountRef(
            account_sid=self._settings.twilio_account_sid, auth_token=self._settings.twilio_auth_token.get_secret_value()
        )

    def _ref(self, account: TelephonyAccount) -> TelephonyAccountRef:
        try:
            return TelephonyAccountRef(account.account_sid, self._cipher.decrypt(account.auth_token_encrypted))
        except SecretDecryptError:
            raise TelephonyCredentialsError("The stored Twilio token could not be read; enter it again") from None

    def _browser_calling(self, account: TelephonyAccount) -> BrowserCalling | None:
        if not (account.api_key_sid and account.api_key_secret_encrypted and account.twiml_app_sid):
            return None
        try:
            secret = self._cipher.decrypt(account.api_key_secret_encrypted)
        except SecretDecryptError:
            return None
        return BrowserCalling(account.api_key_sid, secret, account.twiml_app_sid)

    def _account_values(self, auth_token: str, calling: BrowserCalling | None) -> dict[str, Any]:
        return {
            "auth_token_encrypted": self._cipher.encrypt(auth_token),
            "auth_token_last4": auth_token[-4:],
            "api_key_sid": calling.api_key_sid if calling else None,
            "api_key_secret_encrypted": self._cipher.encrypt(calling.api_key_secret) if calling else None,
            "twiml_app_sid": calling.twiml_app_sid if calling else None,
            "status": "active",
            "last_error_type": None,
            "last_error_at": None,
        }

    def _store_calling(self, account: TelephonyAccount, calling: BrowserCalling) -> None:
        account.api_key_sid = calling.api_key_sid
        account.api_key_secret_encrypted = self._cipher.encrypt(calling.api_key_secret)
        account.twiml_app_sid = calling.twiml_app_sid

    async def _upsert_account(self, existing: TelephonyAccount | None, **values: Any) -> TelephonyAccount:
        account = await self.get_account() if existing is not None else None
        if account is None:
            account = TelephonyAccount(**values)
            await self._accounts.add(account)
        else:
            for name, value in values.items():
                setattr(account, name, value)
        await self._session.flush()
        return account

    async def _create_platform_account(self, actor: User) -> TelephonyAccount:
        """The organisation's subaccount, stored (committed) as soon as Twilio creates it so a failure later in the
        purchase never orphans it and a retry reuses it. Browser calling is set up by the caller."""
        master = self._master()
        await self._session.commit()
        sub = await self._call(
            self._adapters.telephony.create_subaccount(master, f"support-org-{self._org_id}"), None
        )
        values = self._account_values(sub.auth_token, None)
        try:
            async with self._session.begin_nested():
                account = await self._upsert_account(None, kind="platform", account_sid=sub.account_sid, **values)
        except IntegrityError:  # a concurrent first purchase stored its subaccount first: use that one
            account = await self.get_account()
            await self._session.commit()
            await self._close_subaccount(sub.account_sid)
            if account is None:
                raise
            return account
        await self._events.publish(
            TelephonyConnected(
                organisation_id=self._org_id, actor_user_id=actor.id, account_id=account.id, kind="platform"
            )
        )
        await self._session.commit()
        return account

    async def _close_subaccount(self, account_sid: str) -> None:
        """Best effort: an unused platform subaccount is closed so it does not count against the master's limit."""
        try:
            await self._adapters.telephony.close_subaccount(self._master(), account_sid)
        except (TelephonyAuthError, TelephonyError, TelephonyNotConfiguredError) as exc:
            logger.warning(
                "telephony_subaccount_close_failed", organisation_id=str(self._org_id), error_class=type(exc).__name__
            )

    async def _undo_purchase(self, ref: TelephonyAccountRef, provider_sid: str) -> None:
        """A number was bought but never recorded: give it back so the platform does not pay for it."""
        try:
            await self._session.rollback()
        except Exception as exc:  # noqa: BLE001  the original failure is what the caller sees
            logger.warning("phone_number_rollback_failed", organisation_id=str(self._org_id), error_class=type(exc).__name__)
        logger.warning("phone_number_purchase_rolled_back", organisation_id=str(self._org_id), provider_sid=provider_sid)
        try:
            await self._adapters.telephony.release_number(ref, provider_sid)
        except Exception as exc:  # noqa: BLE001  best effort
            logger.error(
                "phone_number_release_failed",
                organisation_id=str(self._org_id),
                provider_sid=provider_sid,
                error_class=type(exc).__name__,
            )
        await self._integration_error("number_unavailable")

    async def _add_number(
        self, account: TelephonyAccount, owned: OwnedNumber, ownership: str, widget_id: UUID | None, url: str, actor: User
    ) -> PhoneNumber:
        number = PhoneNumber(
            telephony_account_id=account.id, e164=owned.e164, provider_sid=owned.provider_sid, ownership=ownership,
            widget_id=widget_id, enabled=True, webhook_url=url,
        )
        try:
            async with self._session.begin_nested():
                await self._numbers.add(number)
                await self._session.flush()
        except IntegrityError:
            raise PhoneNumberUnavailableError("This number is already connected") from None
        await self._events.publish(
            PhoneNumberAdded(
                organisation_id=self._org_id, actor_user_id=actor.id, number_id=number.id, ownership=ownership
            )
        )
        return number

    async def _byo_account(self) -> TelephonyAccount:
        account = await self.get_account()
        if account is None or account.kind != "byo":
            raise TelephonyNotConfiguredError("Connect your Twilio account first")
        return account

    async def _account_for(self, number: PhoneNumber) -> TelephonyAccount:
        account = await self._accounts.get(number.telephony_account_id)
        if account is None:
            raise NotFoundError("Telephony account not found")
        return account

    async def _number(self, number_id: UUID, *, for_update: bool = False) -> PhoneNumber:
        number = await self._numbers.get(number_id, for_update=for_update)
        if number is None:
            raise NotFoundError("Phone number not found")
        return number

    async def _check_phone_lines(self) -> None:
        """The plan has a phone line left (PlanLimitError); checked before any Twilio call."""
        await EntitlementService(self._session, self._org_id, self._adapters).check_capacity("phone_lines")

    async def _check_widget(self, widget_id: UUID | None) -> None:
        if widget_id is not None and await TenantScopedRepository(self._session, Widget, self._org_id).get(widget_id) is None:
            raise NotFoundError("Widget not found")

    async def _call(
        self, call: Awaitable[Any], account: TelephonyAccount | None, *, tolerate_missing: bool = False
    ) -> Any:
        """Awaits one Twilio call (no transaction is open). A rejected token marks the organisation's account
        `auth_failed`; every failure is recorded as a voice integration error and raised as a domain error."""
        try:
            return await call
        except TelephonyAuthError:
            await self._integration_error("auth_failed")
            if account is not None:
                await self._mark_auth_failed(account.id)
                raise TelephonyCredentialsError("Twilio rejected the stored credentials; enter them again") from None
            raise TelephonyNotConfiguredError("Twilio rejected the platform credentials") from None
        except TelephonyError as exc:
            if tolerate_missing and exc.error_type == "not_found":
                return None  # already gone at Twilio: nothing to undo
            await self._provider_failed(exc)

    async def _provider_failed(self, exc: TelephonyError) -> None:
        await self._integration_error(exc.error_type)
        if exc.error_type == "number_unavailable":
            raise PhoneNumberUnavailableError("Twilio could not provide this number") from None
        raise TelephonyUnavailableError("Twilio could not be reached; try again later") from None

    async def _mark_auth_failed(self, account_id: UUID) -> None:
        account = await self._accounts.get(account_id, for_update=True)
        if account is not None:
            account.status, account.last_error_type, account.last_error_at = "auth_failed", "auth_failed", datetime.now(UTC)
        await self._session.commit()  # the request that follows fails; the mark must survive it

    async def _integration_error(self, error_type: str) -> None:
        factory = self._session_factory or async_sessionmaker(bind=self._session.bind, expire_on_commit=False)  # tests
        await record_integration_error(factory, self._org_id, "voice", "twilio", error_type)
