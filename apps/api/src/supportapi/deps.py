from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportapi.container import Container
from supportcore.inbox.notes import NoteService
from supportcore.inbox.publisher import InboxPublisher
from supportcore.inbox.service import InboxService
from supportcore.inbox.tags import TagService
from supportcore.ai.credentials import CredentialService
from supportcore.ai.settings_service import OrganisationAISettingsService
from supportcore.db.models import Membership, Organisation, Role, User, Widget
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.session import use_current_session
from supportcore.errors import NotFoundError
from supportcore.mail.mailbox_service import MailboxService
from supportcore.mail.outbound import OutboundEmailService
from supportcore.voice.numbers_service import TelephonyService
from supportcore.voice.settings_service import VoiceSettingsService
from supportcore.knowledge.document_service import DocumentService
from supportcore.knowledge.kb_service import KnowledgeBaseService
from supportcore.knowledge.playground import PlaygroundService
from supportcore.identity.ports import InvalidTokenError
from supportcore.identity.service import AuthService
from supportcore.observability.context import bind_organisation
from supportcore.orgs.service import OrganisationService
from supportcore.rbac.checker import has_permission
from supportcore.rbac.permissions import Permission
from supportcore.conversations.service import ConversationService
from supportcore.assignment.service import AssignmentService
from supportcore.risk.service import RiskRuleService
from supportcore.support.settings_service import SupportSettingsService
from supportcore.tools.service import BusinessToolService
from supportcore.widget.service import WidgetService
from supportcore.widget.session import WidgetSessionService
from supportcore.widget.tokens import InvalidWidgetTokenError, WidgetClaims

_bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request: commit on success, roll back on error.

    Also published via get_current_session() so event subscribers (e.g. audit log)
    write inside the same transaction. Always depend on it through SessionDep.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        with use_current_session(session):
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise


# scope="function": the commit runs when the endpoint returns, BEFORE the response is sent, so a
# client never reads ahead of the commit and a failed commit is a 500, not a reported success.
# Every dependant must use this alias: FastAPI caches dependencies per (call, scope), so mixing
# scopes would open a second session for the same request.
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_auth_service(session: SessionDep, container: Container = Depends(get_container)) -> AuthService:
    return AuthService(session, container.hasher, container.tokens, container.settings, container.events)


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})


async def get_current_user(
    session: SessionDep,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    container: Container = Depends(get_container),
) -> User:
    if credentials is None:
        raise _unauthorized()
    return await _user_from_credentials(credentials, session, container)


async def get_optional_user(
    session: SessionDep,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    container: Container = Depends(get_container),
) -> User | None:
    """None when no bearer token is sent; a bad token is still 401, never silently anonymous."""
    if credentials is None:
        return None
    return await _user_from_credentials(credentials, session, container)


async def _user_from_credentials(
    credentials: HTTPAuthorizationCredentials, session: AsyncSession, container: Container
) -> User:
    user = await user_from_access_token(session, container, credentials.credentials)
    if user is None:
        raise _unauthorized()
    return user


async def user_from_access_token(session: AsyncSession, container: Container, token: str) -> User | None:
    """The active user behind a dashboard access token; None for any other token (a widget session token,
    an expired or forged one) or a missing or deactivated user."""
    try:
        user_id = container.tokens.decode(token)
    except InvalidTokenError:
        return None
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


PLATFORM_ADMIN_PERMISSIONS = frozenset({Permission.ANALYTICS_VIEW.value})


@dataclass(frozen=True)
class OrgContext:
    organisation: Organisation
    membership: Membership | None
    role: Role | None
    user: User

    @property
    def permissions(self) -> frozenset[str]:
        if self.role is None:  # platform admin viewing an org they are not a member of: read-only
            return PLATFORM_ADMIN_PERMISSIONS
        return frozenset(self.role.permissions)


async def get_org_context(
    session: SessionDep,
    org_id: UUID,
    user: User = Depends(get_current_user),
    container: Container = Depends(get_container),
) -> OrgContext:
    """Resolve the caller's active membership in org_id. Non-members get 404 (NotFoundError), never 403.

    A platform admin who is not a member gets a synthetic read-only context (no membership/role).
    """
    return await resolve_org_context(session, user, org_id, container)


async def resolve_org_context(session: AsyncSession, user: User, org_id: UUID, container: Container) -> OrgContext:
    """get_org_context outside a request (e.g. a WebSocket): NotFoundError for a non-member."""
    service = OrganisationService(session, container.events)
    try:
        organisation, membership, role = await service.get_for_member(user, org_id)
    except NotFoundError:
        organisation = await session.get(Organisation, org_id) if user.is_platform_admin else None
        if organisation is None:
            raise
        bind_organisation(organisation.id)
        return OrgContext(organisation=organisation, membership=None, role=None, user=user)
    bind_organisation(organisation.id)
    return OrgContext(organisation=organisation, membership=membership, role=role, user=user)


def require_permission(perm: Permission) -> Callable[..., Coroutine[Any, Any, OrgContext]]:
    return require_any_permission(perm)


def require_any_permission(*perms: Permission) -> Callable[..., Coroutine[Any, Any, OrgContext]]:
    """The caller needs at least one of perms in the org."""

    async def dependency(ctx: OrgContext = Depends(get_org_context)) -> OrgContext:
        if not any(has_permission(ctx.permissions, p) for p in perms):
            names = " or ".join(p.value for p in perms)
            raise HTTPException(status_code=403, detail=f"Missing permission: {names}")
        return ctx

    return dependency


async def require_platform_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return user


def include_fake_providers(container: Container) -> bool:
    """The test-only `fake` chat/embedding providers exist only when settings.env == "test"."""
    return container.settings.env == "test"


def build_credential_service(session: AsyncSession, organisation_id: UUID, container: Container) -> CredentialService:
    return CredentialService(
        session,
        organisation_id,
        container.adapters.cipher,
        container.adapters.credential_validator,
        container.events,
        allow_private_base_urls=container.settings.effective_allow_private_base_urls(),
    )


def build_ai_settings_service(
    session: AsyncSession, organisation_id: UUID, container: Container
) -> OrganisationAISettingsService:
    return OrganisationAISettingsService(
        session, organisation_id, container.events, include_fake=include_fake_providers(container)
    )


def build_kb_service(session: AsyncSession, organisation_id: UUID, container: Container) -> KnowledgeBaseService:
    return KnowledgeBaseService(session, organisation_id, container.events, container.adapters, container.settings)


def build_widget_service(session: AsyncSession, organisation_id: UUID, container: Container) -> WidgetService:
    return WidgetService(session, organisation_id, container.events, container.adapters.cipher)


def build_support_settings_service(
    session: AsyncSession, organisation_id: UUID, container: Container
) -> SupportSettingsService:
    return SupportSettingsService(session, organisation_id, container.events)


def build_risk_rule_service(session: AsyncSession, organisation_id: UUID, container: Container) -> RiskRuleService:
    return RiskRuleService(session, organisation_id, container.events)


def build_tool_service(session: AsyncSession, organisation_id: UUID, container: Container) -> BusinessToolService:
    return BusinessToolService(
        session,
        organisation_id,
        container.events,
        container.adapters.cipher,
        container.settings,
        container.adapters.tool_executor,
    )


def build_mailbox_service(session: AsyncSession, organisation_id: UUID, container: Container) -> MailboxService:
    return MailboxService(session, organisation_id, container.events, container.adapters, container.settings)


def build_voice_settings_service(
    session: AsyncSession, organisation_id: UUID, container: Container
) -> VoiceSettingsService:
    return VoiceSettingsService(
        session,
        organisation_id,
        container.events,
        include_fake=container.settings.env == "test",
        voice_public_url=container.settings.voice_public_url,
    )


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def build_telephony_service(
    session: AsyncSession,
    organisation_id: UUID,
    container: Container,
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> TelephonyService:
    return TelephonyService(
        session, organisation_id, container.events, container.adapters, container.settings, session_factory=factory
    )


def build_document_service(session: AsyncSession, organisation_id: UUID, container: Container) -> DocumentService:
    return DocumentService(session, organisation_id, container.events, container.adapters, container.settings)


def build_playground_service(session: AsyncSession, organisation_id: UUID, container: Container) -> PlaygroundService:
    return PlaygroundService(
        session,
        organisation_id,
        container.adapters,
        container.adapters.cipher,
        settings=container.settings,
        events=container.events,
    )


def build_conversation_service(session: AsyncSession, organisation_id: UUID, container: Container) -> ConversationService:
    """With the inbox publisher and the bot graph's hand back when the container has an engine (agent actions)."""
    return ConversationService(
        session,
        organisation_id,
        container.events,
        publisher=container.inbox_publisher,
        bot_hand_back=container.turn_runner.hand_back if container.turn_runner is not None else None,
        usage_counter=container.adapters.usage_counter,
    )


def _inbox_publisher(container: Container) -> InboxPublisher:
    if container.inbox_publisher is None:
        raise RuntimeError("Inbox actions need a database engine for the inbox publisher")
    return container.inbox_publisher


def build_outbound_email_service(
    session: AsyncSession, organisation_id: UUID, container: Container
) -> OutboundEmailService:
    return OutboundEmailService(
        session, organisation_id, container.events, container.adapters, _inbox_publisher(container)
    )


def build_note_service(session: AsyncSession, organisation_id: UUID, container: Container) -> NoteService:
    return NoteService(session, organisation_id, publisher=_inbox_publisher(container))


def build_tag_service(session: AsyncSession, organisation_id: UUID, container: Container) -> TagService:
    return TagService(session, organisation_id, publisher=_inbox_publisher(container))


def build_inbox_service(session: AsyncSession, organisation_id: UUID, container: Container) -> InboxService:
    return InboxService(session, organisation_id, container.adapters)


def build_assignment_service(session: AsyncSession, organisation_id: UUID, container: Container) -> AssignmentService:
    return AssignmentService(
        session,
        organisation_id,
        container.events,
        container.adapters,
        container.settings,
        publisher=_inbox_publisher(container),
    )


def build_widget_session_service(session: AsyncSession, container: Container) -> WidgetSessionService:
    return WidgetSessionService(session, container.adapters, container.settings, container.events)


async def get_widget_claims(
    session: SessionDep,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    container: Container = Depends(get_container),
) -> WidgetClaims:
    """The customer behind a widget session token (never a dashboard user). 401 invalid_widget_token
    when the token is missing or invalid, or its widget was deleted or disabled since it was issued.
    Binds the token's organisation for logs and metrics."""
    if credentials is None:
        raise InvalidWidgetTokenError()
    claims = container.widget_tokens.decode(credentials.credentials)
    bind_organisation(claims.organisation_id)
    widget = await TenantScopedRepository(session, Widget, claims.organisation_id).get(claims.widget_id)
    if widget is None or not widget.enabled:
        raise InvalidWidgetTokenError()
    return claims
