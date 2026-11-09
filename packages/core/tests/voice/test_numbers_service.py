import pytest
from sqlalchemy import select
from supportcore.db.models import (
    AuditLog,
    IntegrationEvent,
    PhoneNumber,
    TelephonyAccount,
)
from supportcore.db.session import get_current_session
from supportcore.errors import NotFoundError
from supportcore.voice.errors import (
    PhoneNumberUnavailableError,
    TelephonyAccountInUseError,
    TelephonyCredentialsError,
    TelephonyNotConfiguredError,
)
from supportcore.voice.numbers_service import TelephonyService
from supportcore.voice.telephony.ports import OwnedNumber

from voice.conftest import BAD_TOKEN, BYO_SID, BYO_TOKEN, PLATFORM_SID, PUBLIC_URL

INBOUND = f"{PUBLIC_URL}/twilio/voice/inbound"
TELEPHONY_METHODS = (
    "verify", "create_subaccount", "search_numbers", "buy_number", "list_numbers", "configure_number",
    "release_number", "ensure_browser_calling",
)


@pytest.fixture
def service(session, org, bus, adapters, settings):
    return TelephonyService(session, org.id, bus, adapters, settings)


@pytest.fixture
def service_no_platform(session, org, bus, adapters, settings_no_platform):
    return TelephonyService(session, org.id, bus, adapters, settings_no_platform)


def names(adapters):
    return [c[0] for c in adapters.telephony.calls]


async def test_connect_byo_encrypts_token_and_sets_up_browser_calling(service, owner, adapters, session):
    account = await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    assert (account.kind, account.account_sid, account.status) == ("byo", BYO_SID, "active")
    assert account.auth_token_last4 == BYO_TOKEN[-4:]
    assert BYO_TOKEN not in account.auth_token_encrypted and BYO_TOKEN not in repr(account)
    assert adapters.cipher.decrypt(account.auth_token_encrypted) == BYO_TOKEN
    assert adapters.cipher.decrypt(account.api_key_secret_encrypted)
    assert account.api_key_sid and account.twiml_app_sid
    assert names(adapters) == ["verify", "ensure_browser_calling"]
    assert adapters.telephony.calls[1][1] == (
        BYO_SID, f"{PUBLIC_URL}/twilio/voice/browser", f"{PUBLIC_URL}/twilio/voice/status"
    )  # browser calls report their status through the TwiML App's status callback
    rows = (await session.execute(select(AuditLog).where(AuditLog.action == "telephony.connected"))).scalars().all()
    assert len(rows) == 1 and BYO_TOKEN not in repr(rows[0].metadata_)


async def test_connect_byo_bad_token_is_422_and_nothing_stored(service, owner, session):
    with pytest.raises(TelephonyCredentialsError):
        await service.connect_byo(BYO_SID, BAD_TOKEN, owner)
    assert (await session.execute(select(TelephonyAccount))).scalars().all() == []


@pytest.mark.parametrize("sid,token", [("AC123", BYO_TOKEN), (BYO_SID, "short"), ("XX" + "b" * 32, BYO_TOKEN)])
async def test_connect_byo_format_checks_before_twilio(service, owner, adapters, sid, token):
    with pytest.raises(TelephonyCredentialsError):
        await service.connect_byo(sid, token, owner)
    assert adapters.telephony.calls == []


async def test_connect_needs_public_url(session, org, bus, adapters, settings, owner):
    svc = TelephonyService(session, org.id, bus, adapters, settings.model_copy(update={"voice_public_url": None}))
    with pytest.raises(TelephonyNotConfiguredError):
        await svc.connect_byo(BYO_SID, BYO_TOKEN, owner)


async def test_auth_failure_marks_account_and_records_error(service, owner, adapters, session, org):
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    account = await service.get_account()
    account.auth_token_encrypted = adapters.cipher.encrypt(BAD_TOKEN)
    await session.flush()
    with pytest.raises(TelephonyCredentialsError):
        await service.search("US", "415")
    account = await service.get_account()
    assert (account.status, account.last_error_type) == ("auth_failed", "auth_failed")
    event = (await session.execute(select(IntegrationEvent))).scalar_one()
    assert (event.kind, event.provider, event.error_type) == ("voice", "twilio", "auth_failed")
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)  # saving new credentials resets it
    account = await service.get_account()
    assert (account.status, account.last_error_type) == ("active", None)


async def test_buy_creates_subaccount_once(service, owner, adapters, widget, org):
    first = await service.buy("+15005550007", widget.id, owner)
    second = await service.buy("+15005550008", widget.id, owner)
    assert names(adapters).count("create_subaccount") == 1
    create = next(c for c in adapters.telephony.calls if c[0] == "create_subaccount")
    assert create[1] == (PLATFORM_SID, f"support-org-{org.id}")
    account = await service.get_account()
    assert account.kind == "platform" and account.twiml_app_sid
    app = adapters.telephony.browser_webhooks[account.account_sid]  # the subaccount's TwiML App
    assert (app.voice_url, app.status_callback) == (
        f"{PUBLIC_URL}/twilio/voice/browser", f"{PUBLIC_URL}/twilio/voice/status"
    )
    buys = [c for c in adapters.telephony.calls if c[0] == "buy_number"]
    assert buys[0][1][0] == account.account_sid  # bought on the subaccount, not the master
    assert buys[0][1][2].voice_url == INBOUND and buys[0][1][2].status_callback == f"{PUBLIC_URL}/twilio/voice/status"
    assert (first.ownership, first.webhook_url, first.widget_id, first.enabled) == ("platform", INBOUND, widget.id, True)
    assert second.id != first.id


async def test_buy_without_platform_credentials(service_no_platform, owner, widget, adapters):
    assert service_no_platform.platform_available() is False
    with pytest.raises(TelephonyNotConfiguredError):
        await service_no_platform.buy("+15005550007", widget.id, owner)
    assert adapters.telephony.calls == []


async def test_buy_unknown_widget_is_404(service, owner):
    import uuid

    with pytest.raises(NotFoundError):
        await service.buy("+15005550007", uuid.uuid4(), owner)


async def test_buy_unavailable_number_maps_error(service, owner, widget, adapters):
    from supportcore.voice.telephony.ports import TelephonyError

    async def refuse(*a, **k):
        raise TelephonyError("number_unavailable", 400)

    adapters.telephony.buy_number = refuse
    with pytest.raises(PhoneNumberUnavailableError):
        await service.buy("+15005550007", widget.id, owner)


async def test_search_uses_master_without_account_and_subaccount_after(service, owner, adapters, widget):
    result = await service.search("US", "415")
    assert result[0].e164 == "+15005550007"
    assert adapters.telephony.calls[0][1][0] == PLATFORM_SID
    await service.buy("+15005550007", widget.id, owner)
    account = await service.get_account()
    await service.search("US", None)
    last = [c for c in adapters.telephony.calls if c[0] == "search_numbers"][-1]
    assert last[1][0] == account.account_sid


async def test_connect_number_configures_webhooks(service, owner, adapters, widget, session):
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    listed = await service.list_account_numbers()
    assert [(n.e164, n.connected) for n in listed] == [("+15005550006", False)]
    number = await service.connect_number(listed[0].provider_sid, widget.id, owner)
    assert (number.ownership, number.e164, number.webhook_url) == ("byo", "+15005550006", INBOUND)
    configure = next(c for c in adapters.telephony.calls if c[0] == "configure_number")
    assert configure[1][1] == listed[0].provider_sid and configure[1][2].voice_url == INBOUND
    assert [n.connected for n in await service.list_account_numbers()] == [True]
    with pytest.raises(PhoneNumberUnavailableError):
        await service.connect_number(listed[0].provider_sid, widget.id, owner)
    with pytest.raises(NotFoundError):
        await service.connect_number("PN" + "9" * 32, widget.id, owner)


async def test_connect_number_taken_by_another_org(service, owner, adapters, widget, session, org_b):
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    other = TelephonyAccount(
        organisation_id=org_b.id, kind="byo", account_sid="AC" + "c" * 32, auth_token_encrypted="x", auth_token_last4="xxxx"
    )
    session.add(other)
    await session.flush()
    session.add(PhoneNumber(
        organisation_id=org_b.id, telephony_account_id=other.id, e164="+15005550006", provider_sid="PN" + "1" * 32,
        ownership="byo",
    ))
    await session.flush()
    with pytest.raises(PhoneNumberUnavailableError):
        await service.connect_number("PN" + "0" * 32, widget.id, owner)
    assert "configure_number" not in names(adapters)


async def test_release_platform_releases_at_twilio(service, owner, adapters, widget, session):
    number = await service.buy("+15005550007", widget.id, owner)
    await service.release(number.id, owner)
    assert "release_number" in names(adapters)
    assert (await session.execute(select(PhoneNumber))).scalars().all() == []
    rows = (await session.execute(select(AuditLog).where(AuditLog.action == "phone_number.released"))).scalars().all()
    assert len(rows) == 1


async def test_release_byo_clears_our_webhooks(service, owner, adapters, widget, session):
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    sid = (await service.list_account_numbers())[0].provider_sid
    number = await service.connect_number(sid, widget.id, owner)

    async def ours(account):
        return [OwnedNumber(sid, "+15005550006", INBOUND)]

    adapters.telephony.list_numbers = ours
    await service.release(number.id, owner)
    configure = [c for c in adapters.telephony.calls if c[0] == "configure_number"][-1]
    assert configure[1][2] is None and "release_number" not in names(adapters)
    assert (await session.execute(select(PhoneNumber))).scalars().all() == []


async def test_release_byo_leaves_foreign_webhooks(service, owner, adapters, widget, session):
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    sid = (await service.list_account_numbers())[0].provider_sid
    number = await service.connect_number(sid, widget.id, owner)

    async def foreign(account):
        return [OwnedNumber(sid, "+15005550006", "https://someone-else.example.com/voice")]

    adapters.telephony.list_numbers = foreign
    before = names(adapters).count("configure_number")
    await service.release(number.id, owner)
    assert names(adapters).count("configure_number") == before
    assert (await session.execute(select(PhoneNumber))).scalars().all() == []


async def test_sync_webhooks_updates_changed_urls_only(session, org, bus, adapters, settings, owner, widget):
    old = TelephonyService(
        session, org.id, bus, adapters, settings.model_copy(update={"voice_public_url": "https://old.example.com"})
    )
    stale = await old.buy("+15005550007", widget.id, owner)
    service = TelephonyService(session, org.id, bus, adapters, settings)
    current = await service.buy("+15005550008", widget.id, owner)
    assert stale.webhook_url == "https://old.example.com/twilio/voice/inbound" and current.webhook_url == INBOUND
    adapters.telephony.calls.clear()
    assert await service.sync_webhooks() == 1
    configured = [c for c in adapters.telephony.calls if c[0] == "configure_number"]
    assert [c[1][1] for c in configured] == [stale.provider_sid]
    assert configured[0][1][2].voice_url == INBOUND
    refreshed = next(c for c in adapters.telephony.calls if c[0] == "ensure_browser_calling")  # the TwiML App
    assert refreshed[1][1:] == (f"{PUBLIC_URL}/twilio/voice/browser", f"{PUBLIC_URL}/twilio/voice/status")
    await session.refresh(stale)
    assert stale.webhook_url == INBOUND
    assert await service.sync_webhooks() == 0


async def test_sync_webhooks_needs_public_url(session, org, bus, adapters, settings):
    svc = TelephonyService(session, org.id, bus, adapters, settings.model_copy(update={"voice_public_url": None}))
    with pytest.raises(TelephonyNotConfiguredError):
        await svc.sync_webhooks()


async def test_update_number(service, owner, widget, adapters):
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    sid = (await service.list_account_numbers())[0].provider_sid
    number = await service.connect_number(sid, None, owner)
    updated = await service.update_number(number.id, label="Main line", widget_id=widget.id, enabled=False)
    assert (updated.label, updated.widget_id, updated.enabled) == ("Main line", widget.id, False)
    cleared = await service.update_number(number.id, widget_id=None)
    assert cleared.widget_id is None and cleared.label == "Main line"
    import uuid

    with pytest.raises(NotFoundError):
        await service.update_number(number.id, widget_id=uuid.uuid4())
    with pytest.raises(NotFoundError):
        await service.update_number(uuid.uuid4(), label="x")


async def test_disconnect_clears_ours_and_deletes(service, owner, adapters, widget, session):
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    sid = (await service.list_account_numbers())[0].provider_sid
    await service.connect_number(sid, widget.id, owner)

    async def mixed(account):
        return [OwnedNumber(sid, "+15005550006", INBOUND), OwnedNumber("PN" + "5" * 32, "+15005550009", "https://x.example")]

    adapters.telephony.list_numbers = mixed
    await service.disconnect(owner)
    configured = [c for c in adapters.telephony.calls if c[0] == "configure_number"]
    assert [c[1][1] for c in configured][-1] == sid and configured[-1][1][2] is None
    assert len([c for c in configured if c[1][2] is None]) == 1
    assert (await session.execute(select(TelephonyAccount))).scalars().all() == []
    assert (await session.execute(select(PhoneNumber))).scalars().all() == []
    with pytest.raises(NotFoundError):
        await service.disconnect(owner)


async def test_disconnect_platform_refused(service, owner, widget):
    await service.buy("+15005550007", widget.id, owner)
    with pytest.raises(TelephonyNotConfiguredError):
        await service.disconnect(owner)


async def test_no_session_open_during_twilio_calls(service, owner, adapters, widget, session):
    states: list[tuple[str, bool]] = []
    for name in TELEPHONY_METHODS:
        original = getattr(adapters.telephony, name)

        async def wrapper(*args, _original=original, _name=name, **kwargs):
            states.append((_name, get_current_session().in_transaction()))
            return await _original(*args, **kwargs)

        setattr(adapters.telephony, name, wrapper)
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    await service.list_account_numbers()
    await service.search("US", None)
    sid = (await service.list_account_numbers())[0].provider_sid
    number = await service.connect_number(sid, widget.id, owner)
    await service.sync_webhooks()
    await service.release(number.id, owner)
    await service.disconnect(owner)
    await service.search("US", None)  # no account yet: the platform's master
    bought = await service.buy("+15005550007", widget.id, owner)
    await service.release(bought.id, owner)
    assert len(states) >= 14
    assert {"create_subaccount", "buy_number", "release_number", "search_numbers"} <= {n for n, _ in states}
    assert [name for name, open_ in states if open_] == []


async def _other_org_holding(session, org_b, e164, sid="AC" + "c" * 32):
    other = TelephonyAccount(
        organisation_id=org_b.id, kind="byo", account_sid=sid, auth_token_encrypted="x", auth_token_last4="xxxx"
    )
    session.add(other)
    await session.flush()
    session.add(PhoneNumber(
        organisation_id=org_b.id, telephony_account_id=other.id, e164=e164, provider_sid="PN" + "1" * 32, ownership="byo",
    ))
    await session.flush()
    await session.commit()


async def test_purchase_released_when_row_cannot_be_saved(service, owner, adapters, widget, session, org_b):
    await _other_org_holding(session, org_b, "+15005550007")  # the unique e164 insert will fail
    with pytest.raises(PhoneNumberUnavailableError):
        await service.buy("+15005550007", widget.id, owner)
    released = [c for c in adapters.telephony.calls if c[0] == "release_number"]
    bought = next(c for c in adapters.telephony.calls if c[0] == "buy_number")
    assert len(released) == 1
    event = (await session.execute(select(IntegrationEvent))).scalar_one()
    assert (event.kind, event.provider, event.error_type) == ("voice", "twilio", "number_unavailable")
    assert adapters.telephony.numbers[bought[1][0]] == []


async def test_purchase_released_when_audit_write_fails(service, owner, adapters, widget, session):
    async def boom(event):
        if type(event).__name__ == "PhoneNumberAdded":
            raise RuntimeError("audit down")

    service._events.publish, real = boom, service._events.publish
    with pytest.raises(RuntimeError):
        await service.buy("+15005550007", widget.id, owner)
    service._events.publish = real
    assert names(adapters).count("release_number") == 1
    assert (await session.execute(select(PhoneNumber))).scalars().all() == []


async def test_purchase_row_is_committed_by_the_service(service, owner, widget, session):
    commits = []
    original = session.commit

    async def counting():
        commits.append(1)
        await original()

    session.commit = counting
    await service.buy("+15005550007", widget.id, owner)
    assert commits and not session.in_transaction()
    assert (await session.execute(select(PhoneNumber))).scalar_one()


async def test_subaccount_is_stored_before_browser_calling_setup(service, owner, adapters, widget, session):
    from supportcore.voice.errors import TelephonyUnavailableError
    from supportcore.voice.telephony.ports import TelephonyError

    real = adapters.telephony.ensure_browser_calling

    async def fail(*a, **k):
        raise TelephonyError("connection_failed", 500)

    adapters.telephony.ensure_browser_calling = fail
    with pytest.raises(TelephonyUnavailableError):
        await service.buy("+15005550007", widget.id, owner)
    account = await service.get_account()
    assert account is not None and account.kind == "platform" and account.twiml_app_sid is None
    adapters.telephony.ensure_browser_calling = real
    number = await service.buy("+15005550007", widget.id, owner)
    assert names(adapters).count("create_subaccount") == 1  # the retry reused the stored subaccount
    assert (await service.get_account()).twiml_app_sid and number.ownership == "platform"


async def test_losing_subaccount_race_closes_the_fresh_subaccount(service, owner, adapters, widget, session, org):
    real = adapters.telephony.create_subaccount

    async def raced(master, friendly_name):
        fresh = await real(master, friendly_name)
        session.add(TelephonyAccount(
            organisation_id=org.id, kind="platform", account_sid="AC" + "d" * 32,
            auth_token_encrypted=adapters.cipher.encrypt("w" * 32), auth_token_last4="wwww",
        ))
        await session.commit()
        return fresh

    adapters.telephony.create_subaccount = raced
    number = await service.buy("+15005550007", widget.id, owner)
    closed = [c for c in adapters.telephony.calls if c[0] == "close_subaccount"]
    created = next(c for c in adapters.telephony.calls if c[0] == "create_subaccount")
    assert len(closed) == 1 and closed[0][1][0] == PLATFORM_SID and closed[0][1][1] != "AC" + "d" * 32
    assert (await service.get_account()).account_sid == "AC" + "d" * 32 and number.ownership == "platform" and created


async def test_connect_byo_sid_of_another_org_is_refused_before_twilio(service, owner, adapters, session, org_b):
    await _other_org_holding(session, org_b, "+15005550009", sid=BYO_SID)
    with pytest.raises(TelephonyAccountInUseError):
        await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    assert adapters.telephony.calls == []


async def test_connect_byo_over_empty_platform_account_closes_the_subaccount(service, owner, adapters, widget):
    bought = await service.buy("+15005550007", widget.id, owner)
    platform_sid = (await service.get_account()).account_sid
    with pytest.raises(TelephonyAccountInUseError):  # numbers still there
        await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    await service.release(bought.id, owner)
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    closed = [c for c in adapters.telephony.calls if c[0] == "close_subaccount"]
    assert [(c[1][0], c[1][1]) for c in closed] == [(PLATFORM_SID, platform_sid)]
    assert (await service.get_account()).kind == "byo"


async def test_swapping_byo_account_with_numbers_is_a_conflict(service, owner, widget):
    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    sid = (await service.list_account_numbers())[0].provider_sid
    await service.connect_number(sid, widget.id, owner)
    with pytest.raises(TelephonyAccountInUseError):
        await service.connect_byo("AC" + "e" * 32, BYO_TOKEN, owner)


async def test_release_of_number_twilio_no_longer_has_counts_as_released(service, owner, adapters, widget, session):
    from supportcore.voice.telephony.ports import TelephonyError

    number = await service.buy("+15005550007", widget.id, owner)

    async def gone(*a, **k):
        raise TelephonyError("not_found", 404)

    adapters.telephony.release_number = gone
    await service.release(number.id, owner)
    assert (await session.execute(select(PhoneNumber))).scalars().all() == []
    assert (await session.execute(select(IntegrationEvent))).scalars().all() == []


async def test_number_buy_refused_before_twilio_call(service, owner, adapters, session, org):
    from supportcore.billing.errors import PlanLimitError
    from supportcore.billing.fake_billing import FakeBilling
    from supportcore.testing import start_trial_plan

    await start_trial_plan(session, org.id, phone_lines=0)
    adapters.billing = FakeBilling()
    with pytest.raises(PlanLimitError) as raised:
        await service.buy("+15005550006", None, owner)
    assert (raised.value.resource, raised.value.limit) == ("phone_lines", 0)
    assert adapters.telephony.calls == []  # no subaccount, no purchase
    assert (await session.execute(select(TelephonyAccount))).scalars().all() == []


async def test_connect_number_refused_at_phone_line_limit(service, owner, adapters, session, org):
    from supportcore.billing.errors import PlanLimitError
    from supportcore.billing.fake_billing import FakeBilling
    from supportcore.testing import start_trial_plan

    await service.connect_byo(BYO_SID, BYO_TOKEN, owner)
    await start_trial_plan(session, org.id, phone_lines=0)
    adapters.billing = FakeBilling()
    before = names(adapters)
    with pytest.raises(PlanLimitError):
        await service.connect_number("PN" + "1" * 32, None, owner)
    assert names(adapters) == before  # Twilio was not asked for the account's numbers
