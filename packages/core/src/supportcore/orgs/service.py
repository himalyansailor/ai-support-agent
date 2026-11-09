import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.catalog import ensure_supported_language
from supportcore.billing.accounts import load_account
from supportcore.db.models import Membership, Organisation, Role, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.orgs.events import OrganisationCreated, OrganisationUpdated
from supportcore.rbac.defaults import DEFAULT_ROLES

SLUG_MAX_LENGTH = 50
SLUG_FALLBACK = "org"
_MAX_SLUG_ATTEMPTS = 1000


def slugify(name: str) -> str:
    """Lowercase ASCII, non-alphanumerics become single dashes, trimmed, max 50 chars, never empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:SLUG_MAX_LENGTH].strip("-")
    return slug or SLUG_FALLBACK


class OrganisationService:
    def __init__(self, session: AsyncSession, events: EventBus) -> None:
        self._session = session
        self._events = events

    async def create(self, creator: User, name: str) -> Organisation:
        name = name.strip()
        org = await self._insert_with_unique_slug(creator, name)

        roles = TenantScopedRepository(self._session, Role, org.id)
        owner_role: Role | None = None
        for role_name, permissions in DEFAULT_ROLES.items():
            role = Role(name=role_name, permissions=sorted(p.value for p in permissions), is_system=True)
            await roles.add(role)
            if role_name == "Owner":
                owner_role = role
        await self._session.flush()
        assert owner_role is not None

        await TenantScopedRepository(self._session, Membership, org.id).add(
            Membership(user_id=creator.id, role_id=owner_role.id, status="active")
        )
        await self._session.flush()
        await load_account(self._session, org.id)  # creates the organisation's billing account (no plan yet)

        await self._events.publish(
            OrganisationCreated(organisation_id=org.id, actor_user_id=creator.id, organisation_name=name)
        )
        return org

    async def set_language(self, org: Organisation, language: str, actor: User) -> Organisation:
        """Raises UnsupportedLanguageError unless `language` is in the catalog's LANGUAGES."""
        language = ensure_supported_language(language.strip())
        if org.language != language:
            changes = {"language": {"from": org.language, "to": language}}
            org.language = language
            await self._session.flush()
            await self._events.publish(
                OrganisationUpdated(organisation_id=org.id, actor_user_id=actor.id, changes=changes)
            )
        return org

    async def get_for_member(self, user: User, org_id: UUID) -> tuple[Organisation, Membership, Role]:
        """Active members only; anyone else (non-member, deactivated, unknown org) gets NotFoundError."""
        memberships = TenantScopedRepository(self._session, Membership, org_id)
        found = await memberships.list(Membership.user_id == user.id, Membership.status == "active", limit=1)
        org = await self._session.get(Organisation, org_id)
        if not found or org is None:
            raise NotFoundError("Organisation not found")
        membership = found[0]
        role = await TenantScopedRepository(self._session, Role, org_id).get(membership.role_id)
        if role is None:  # unreachable while the FK holds; fail closed
            raise NotFoundError("Organisation not found")
        return org, membership, role

    async def _slug_taken(self, slug: str) -> bool:
        return (
            await self._session.execute(select(Organisation.id).where(Organisation.slug == slug))
        ).first() is not None

    async def _insert_with_unique_slug(self, creator: User, name: str) -> Organisation:
        base = slugify(name)
        for attempt in range(1, _MAX_SLUG_ATTEMPTS + 1):
            slug = base if attempt == 1 else f"{base}-{attempt}"
            if await self._slug_taken(slug):
                continue
            org = Organisation(name=name, slug=slug, created_by_user_id=creator.id)
            try:
                async with self._session.begin_nested():
                    self._session.add(org)
            except IntegrityError:
                # Lost a race for this slug to a concurrent request: try the next suffix.
                # Anything else (e.g. a bad creator id) is a real error.
                if not await self._slug_taken(slug):
                    raise
                continue
            return org
        raise RuntimeError("could not allocate a unique organisation slug")
