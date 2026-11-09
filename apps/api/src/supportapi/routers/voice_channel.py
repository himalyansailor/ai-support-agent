"""Voice channel admin: settings, readiness, the Twilio account and phone numbers (channels.manage)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from supportcore.db.models import PhoneNumber, TelephonyAccount, VoiceSettings
from supportcore.ratelimit.ports import enforce
from supportcore.rbac.permissions import Permission
from supportcore.voice.catalog import (
    RealtimeModelSpec,
    SttModelSpec,
    TtsModelSpec,
    credential_provider_for,
    realtime_models,
    stt_models,
    tts_models,
)
from supportcore.voice.numbers_service import TelephonyService

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    SessionFactoryDep,
    build_telephony_service,
    build_voice_settings_service,
    get_container,
    require_permission,
)
from supportapi.schemas.voice_channel import (
    AccountNumberOut,
    AvailableNumberOut,
    BuyNumberRequest,
    ConnectNumberRequest,
    ConnectTelephonyRequest,
    PhoneNumberOut,
    SttModelOut,
    SyncWebhooksOut,
    TelephonyNotConnectedOut,
    TelephonyOut,
    TtsModelOut,
    UpdateNumberRequest,
    UpdateVoiceSettingsRequest,
    VoiceCatalogOut,
    VoiceReadinessOut,
    VoiceSettingsOut,
)

router = APIRouter(prefix="/orgs/{org_id}/voice", tags=["voice-channel"])

SEARCH_PER_ORG = (20, 60)
BUY_PER_ORG = (5, 60)

Manage = Depends(require_permission(Permission.CHANNELS_MANAGE))


def _settings_out(row: VoiceSettings) -> VoiceSettingsOut:
    return VoiceSettingsOut(**{name: getattr(row, name) for name in VoiceSettingsOut.model_fields})


def _number_out(service: TelephonyService, number: PhoneNumber) -> PhoneNumberOut:
    return PhoneNumberOut(
        id=number.id,
        e164=number.e164,
        label=number.label,
        widget_id=number.widget_id,
        enabled=number.enabled,
        ownership=number.ownership,
        webhooks_out_of_date=service.webhooks_out_of_date(number),
    )


def _telephony_out(service: TelephonyService, account: TelephonyAccount | None) -> TelephonyOut | TelephonyNotConnectedOut:
    flags = {"platform_available": service.platform_available(), "public_url_set": service.inbound_url() is not None}
    if account is None:
        return TelephonyNotConnectedOut(**flags)
    return TelephonyOut(
        kind=account.kind,
        account_sid=account.account_sid,
        auth_token_last4=account.auth_token_last4,
        status=account.status,
        last_error_type=account.last_error_type,
        browser_calling_ready=bool(account.api_key_sid and account.api_key_secret_encrypted and account.twiml_app_sid),
        **flags,
    )


# --- settings ---------------------------------------------------------------------------------------


@router.get("/settings", response_model=VoiceSettingsOut)
async def get_settings(
    session: SessionDep, ctx: OrgContext = Manage, container: Container = Depends(get_container)
) -> VoiceSettingsOut:
    return _settings_out(await build_voice_settings_service(session, ctx.organisation.id, container).get())


@router.put("/settings", response_model=VoiceSettingsOut)
async def update_settings(
    session: SessionDep,
    body: UpdateVoiceSettingsRequest,
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> VoiceSettingsOut:
    service = build_voice_settings_service(session, ctx.organisation.id, container)
    return _settings_out(await service.update(ctx.user, **body.model_dump(exclude_unset=True)))


@router.get("/readiness", response_model=VoiceReadinessOut)
async def get_readiness(
    session: SessionDep, ctx: OrgContext = Manage, container: Container = Depends(get_container)
) -> VoiceReadinessOut:
    readiness = await build_voice_settings_service(session, ctx.organisation.id, container).readiness()
    return VoiceReadinessOut(phone=readiness.phone, browser=readiness.browser, reasons=list(readiness.reasons))


@router.get("/catalog", response_model=VoiceCatalogOut)
async def get_catalog(ctx: OrgContext = Manage, container: Container = Depends(get_container)) -> VoiceCatalogOut:
    language = ctx.organisation.language
    include_fake = container.settings.env == "test"

    def common(s: SttModelSpec | TtsModelSpec | RealtimeModelSpec) -> dict[str, object]:
        return {
            "provider": s.provider,
            "model": s.model,
            "label": s.label,
            "recommended": s.recommended,
            "requires_credential": s.requires_credential,
            "credential_provider": credential_provider_for(s.provider),
        }

    return VoiceCatalogOut(
        language=language,
        stt=[SttModelOut(**common(s)) for s in stt_models(language, include_fake)],
        tts=[
            TtsModelOut(**common(s), voices=list(s.voices), default_voice=s.default_voice)
            for s in tts_models(language, include_fake)
        ],
        realtime=[
            TtsModelOut(**common(s), voices=list(s.voices), default_voice=s.default_voice)
            for s in realtime_models(language, include_fake)
        ],
    )


# --- the Twilio account -----------------------------------------------------------------------------


@router.get("/telephony", response_model=TelephonyOut | TelephonyNotConnectedOut)
async def get_telephony(
    session: SessionDep, factory: SessionFactoryDep, ctx: OrgContext = Manage, container: Container = Depends(get_container)
) -> TelephonyOut | TelephonyNotConnectedOut:
    service = build_telephony_service(session, ctx.organisation.id, container, factory)
    return _telephony_out(service, await service.get_account())


@router.put("/telephony", response_model=TelephonyOut)
async def connect_telephony(
    session: SessionDep,
    factory: SessionFactoryDep,
    body: ConnectTelephonyRequest,
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> TelephonyOut:
    service = build_telephony_service(session, ctx.organisation.id, container, factory)
    return _telephony_out(service, await service.connect_byo(body.account_sid, body.auth_token, ctx.user))


@router.delete("/telephony", status_code=204)
async def disconnect_telephony(
    session: SessionDep, factory: SessionFactoryDep, ctx: OrgContext = Manage, container: Container = Depends(get_container)
) -> Response:
    await build_telephony_service(session, ctx.organisation.id, container, factory).disconnect(ctx.user)
    return Response(status_code=204)


# --- numbers ----------------------------------------------------------------------------------------


@router.get("/numbers", response_model=list[PhoneNumberOut])
async def list_numbers(
    session: SessionDep, factory: SessionFactoryDep, ctx: OrgContext = Manage, container: Container = Depends(get_container)
) -> list[PhoneNumberOut]:
    service = build_telephony_service(session, ctx.organisation.id, container, factory)
    return [_number_out(service, n) for n in await service.list_numbers()]


@router.get("/numbers/search", response_model=list[AvailableNumberOut])
async def search_numbers(
    session: SessionDep,
    factory: SessionFactoryDep,
    country: str = Query(pattern=r"^[A-Z]{2}$"),
    area_code: str | None = Query(default=None, pattern=r"^[0-9]{1,10}$"),
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> list[AvailableNumberOut]:
    await enforce(container.adapters.rate_limiter, f"org:{ctx.organisation.id}:voice-search", SEARCH_PER_ORG)
    found = await build_telephony_service(session, ctx.organisation.id, container, factory).search(country, area_code)
    return [AvailableNumberOut(**vars(n)) for n in found]


@router.post("/numbers/buy", response_model=PhoneNumberOut, status_code=201)
async def buy_number(
    session: SessionDep,
    factory: SessionFactoryDep,
    body: BuyNumberRequest,
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> PhoneNumberOut:
    await enforce(container.adapters.rate_limiter, f"org:{ctx.organisation.id}:voice-buy", BUY_PER_ORG)
    service = build_telephony_service(session, ctx.organisation.id, container, factory)
    return _number_out(service, await service.buy(body.e164, body.widget_id, ctx.user))


@router.get("/numbers/available", response_model=list[AccountNumberOut])
async def available_numbers(
    session: SessionDep, factory: SessionFactoryDep, ctx: OrgContext = Manage, container: Container = Depends(get_container)
) -> list[AccountNumberOut]:
    service = build_telephony_service(session, ctx.organisation.id, container, factory)
    return [AccountNumberOut(**vars(n)) for n in await service.list_account_numbers()]


@router.post("/numbers/connect", response_model=PhoneNumberOut, status_code=201)
async def connect_number(
    session: SessionDep,
    factory: SessionFactoryDep,
    body: ConnectNumberRequest,
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> PhoneNumberOut:
    service = build_telephony_service(session, ctx.organisation.id, container, factory)
    return _number_out(service, await service.connect_number(body.provider_sid, body.widget_id, ctx.user))


@router.post("/numbers/sync-webhooks", response_model=SyncWebhooksOut)
async def sync_webhooks(
    session: SessionDep, factory: SessionFactoryDep, ctx: OrgContext = Manage, container: Container = Depends(get_container)
) -> SyncWebhooksOut:
    updated = await build_telephony_service(session, ctx.organisation.id, container, factory).sync_webhooks()
    return SyncWebhooksOut(updated=updated)


@router.patch("/numbers/{number_id}", response_model=PhoneNumberOut)
async def update_number(
    session: SessionDep,
    factory: SessionFactoryDep,
    number_id: UUID,
    body: UpdateNumberRequest,
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> PhoneNumberOut:
    service = build_telephony_service(session, ctx.organisation.id, container, factory)
    number = await service.update_number(number_id, **body.model_dump(exclude_unset=True))
    return _number_out(service, number)


@router.delete("/numbers/{number_id}", status_code=204)
async def release_number(
    session: SessionDep,
    factory: SessionFactoryDep,
    number_id: UUID,
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> Response:
    await build_telephony_service(session, ctx.organisation.id, container, factory).release(number_id, ctx.user)
    return Response(status_code=204)
