"""Model catalog, org API keys (credentials) and org AI settings."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    build_ai_settings_service,
    build_credential_service,
    get_container,
    get_current_user,
    get_org_context,
    include_fake_providers,
    require_any_permission,
    require_permission,
)
from supportapi.schemas.ai import (
    AISettingsOut,
    AISettingsRequest,
    CatalogOut,
    ChatModelOut,
    CreateCredentialRequest,
    CredentialOut,
    EmbeddingModelOut,
    LanguageOut,
)
from supportcore.ai import catalog
from supportcore.ai.settings_service import OrganisationAISettingsService
from supportcore.db.models import OrganisationAISettings, User
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["ai"])


@router.get("/ai/catalog", response_model=CatalogOut)
async def get_catalog(
    language: str = "en",
    _user: User = Depends(get_current_user),
    container: Container = Depends(get_container),
) -> CatalogOut:
    catalog.ensure_supported_language(language)
    include_fake = include_fake_providers(container)
    return CatalogOut(
        chat_models=[
            ChatModelOut(
                provider=m.provider,
                model=m.model,
                label=m.label,
                recommended=m.recommended,
                requires_credential=m.requires_credential,
            )
            for m in catalog.chat_models(include_fake=include_fake)
        ],
        embedding_models=[
            EmbeddingModelOut(
                provider=m.provider,
                model=m.model,
                label=m.label,
                dimensions=m.dimensions,
                requires_credential=m.requires_credential,
                recommended=m.recommended,
            )
            for m in catalog.embedding_models_for(language, include_fake=include_fake)
        ],
        languages=[LanguageOut(code=code, label=label) for code, label in catalog.LANGUAGES],
    )


# --- credentials (list: billing.manage, bot.configure or kb.manage (KB forms pick a key); create/delete: billing.manage) ------------------------------------------------------------


@router.get("/orgs/{org_id}/ai/credentials", response_model=list[CredentialOut])
async def list_credentials(
    session: SessionDep,
    ctx: OrgContext = Depends(
        require_any_permission(Permission.BILLING_MANAGE, Permission.BOT_CONFIGURE, Permission.KB_MANAGE)
    ),
    container: Container = Depends(get_container),
) -> list[CredentialOut]:
    rows = await build_credential_service(session, ctx.organisation.id, container).list()
    return [CredentialOut.model_validate(r) for r in rows]


@router.post("/orgs/{org_id}/ai/credentials", response_model=CredentialOut, status_code=201)
async def create_credential(
    session: SessionDep,
    body: CreateCredentialRequest,
    ctx: OrgContext = Depends(require_permission(Permission.BILLING_MANAGE)),
    container: Container = Depends(get_container),
) -> CredentialOut:
    service = build_credential_service(session, ctx.organisation.id, container)
    row = await service.create(body.provider, body.label, body.api_key, body.base_url, ctx.user)
    return CredentialOut.model_validate(row)


@router.delete("/orgs/{org_id}/ai/credentials/{credential_id}", status_code=204)
async def delete_credential(
    session: SessionDep,
    credential_id: UUID,
    ctx: OrgContext = Depends(require_permission(Permission.BILLING_MANAGE)),
    container: Container = Depends(get_container),
) -> Response:
    await build_credential_service(session, ctx.organisation.id, container).delete(credential_id, ctx.user)
    return Response(status_code=204)


# --- AI settings (read: any member, write: bot.configure) ------------------------------------


async def _settings_out(service: OrganisationAISettingsService, row: OrganisationAISettings) -> AISettingsOut:
    return AISettingsOut(
        chat_provider=row.chat_provider,
        chat_model=row.chat_model,
        chat_credential_id=row.chat_credential_id,
        temperature=row.temperature,
        ready=await service.is_ready(row),
    )


@router.get("/orgs/{org_id}/ai/settings", response_model=AISettingsOut)
async def get_ai_settings(
    session: SessionDep,
    ctx: OrgContext = Depends(get_org_context),
    container: Container = Depends(get_container),
) -> AISettingsOut:
    service = build_ai_settings_service(session, ctx.organisation.id, container)
    return await _settings_out(service, await service.get())


@router.put("/orgs/{org_id}/ai/settings", response_model=AISettingsOut)
async def put_ai_settings(
    session: SessionDep,
    body: AISettingsRequest,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> AISettingsOut:
    service = build_ai_settings_service(session, ctx.organisation.id, container)
    row = await service.update(
        body.chat_provider, body.chat_model, body.chat_credential_id, body.temperature, ctx.user
    )
    return await _settings_out(service, row)
