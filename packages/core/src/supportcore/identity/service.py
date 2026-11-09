import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from weakref import WeakKeyDictionary

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.config import Settings
from supportcore.db.models import RefreshToken, User
from supportcore.events import DomainEvent, EventBus
from supportcore.identity.ports import AccessTokenService, PasswordHasher

MIN_PASSWORD_LENGTH = 10
REFRESH_TOKEN_BYTES = 32
REFRESH_REUSE_GRACE_SECONDS = 10  # concurrent refreshes (several tabs) within this window are benign


class EmailTakenError(Exception):
    def __init__(self) -> None:
        super().__init__("Email is already registered")


class InvalidCredentialsError(Exception):
    def __init__(self) -> None:
        super().__init__("Invalid email or password")


class InvalidRefreshTokenError(Exception):
    def __init__(self) -> None:
        super().__init__("Invalid refresh token")


class WeakPasswordError(Exception):
    def __init__(self) -> None:
        super().__init__(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


@dataclass(frozen=True, kw_only=True)
class UserSignedUp(DomainEvent):
    user_id: uuid.UUID
    email: str


@dataclass(frozen=True)
class AuthResult:
    user: User
    access_token: str
    refresh_token: str
    refresh_expires_at: datetime


def normalise_email(email: str) -> str:
    return email.strip().lower()


def _hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# One dummy hash per hasher instance, so unknown-email logins still pay for a verify.
_dummy_hashes: "WeakKeyDictionary[PasswordHasher, str]" = WeakKeyDictionary()


def _dummy_hash(hasher: PasswordHasher) -> str:
    try:
        return _dummy_hashes[hasher]
    except KeyError:
        return _dummy_hashes.setdefault(hasher, hasher.hash(secrets.token_urlsafe(16)))


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        hasher: PasswordHasher,
        tokens: AccessTokenService,
        settings: Settings,
        events: EventBus,
    ) -> None:
        self._session = session
        self._hasher = hasher
        self._tokens = tokens
        self._settings = settings
        self._events = events

    async def signup(self, email: str, password: str, name: str) -> AuthResult:
        email = normalise_email(email)
        if len(password) < MIN_PASSWORD_LENGTH:
            raise WeakPasswordError()
        if await self._find_user(email) is not None:
            raise EmailTakenError()
        password_hash = await asyncio.to_thread(self._hasher.hash, password)
        user = User(email=email, password_hash=password_hash, name=name.strip())
        try:
            async with self._session.begin_nested():
                self._session.add(user)
        except IntegrityError:  # lost a race with a concurrent signup for the same email
            raise EmailTakenError() from None
        await self._events.publish(UserSignedUp(actor_user_id=user.id, user_id=user.id, email=email))
        return await self._issue(user, family_id=uuid.uuid4())

    async def login(self, email: str, password: str) -> AuthResult:
        user = await self._find_user(normalise_email(email))
        if user is None:
            await asyncio.to_thread(self._hasher.verify, _dummy_hash(self._hasher), password)
            raise InvalidCredentialsError()
        ok = await asyncio.to_thread(self._hasher.verify, user.password_hash, password)
        if not ok or not user.is_active:
            raise InvalidCredentialsError()
        return await self._issue(user, family_id=uuid.uuid4())

    async def refresh(self, raw_refresh_token: str) -> AuthResult:
        token = await self._find_refresh_token(raw_refresh_token, for_update=True)
        if token is None:
            raise InvalidRefreshTokenError()
        now = datetime.now(UTC)
        if token.revoked_at is not None:
            if await self._is_benign_race(token, now):
                # Another tab rotated this token a moment ago: hand this caller a sibling in the
                # same family instead of treating it as theft. The presented token stays revoked.
                user = await self._active_user(token)
                return await self._issue(user, family_id=token.family_id)
            # Reuse of a rotated/revoked token: assume theft and kill the whole family.
            # Commit now: the caller's transaction is rolled back when the error propagates.
            await self._revoke_family(token.family_id, now)
            await self._session.commit()
            raise InvalidRefreshTokenError()
        if token.expires_at <= now:
            raise InvalidRefreshTokenError()
        user = await self._active_user(token)
        result, new_token = await self._issue_with_row(user, family_id=token.family_id)
        token.revoked_at = now
        token.replaced_by_id = new_token.id
        await self._session.flush()
        return result

    async def logout(self, raw_refresh_token: str) -> None:
        token = await self._find_refresh_token(raw_refresh_token, for_update=True)
        if token is not None:
            await self._revoke_family(token.family_id, datetime.now(UTC))

    # --- internals ---------------------------------------------------------

    async def _find_user(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _find_refresh_token(self, raw: str, *, for_update: bool) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == _hash_refresh_token(raw))
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _active_user(self, token: RefreshToken) -> User:
        user = await self._session.get(User, token.user_id)
        if user is None or not user.is_active:
            raise InvalidRefreshTokenError()
        return user

    async def _is_benign_race(self, token: RefreshToken, now: datetime) -> bool:
        """A token revoked by ROTATION within the grace window, whose family is still alive.

        Logout and theft detection revoke the whole family (and never set replaced_by_id), so a
        logged-out or compromised family is never resurrected through this path.
        """
        if token.replaced_by_id is None or token.revoked_at is None or token.expires_at <= now:
            return False
        if now - token.revoked_at >= timedelta(seconds=REFRESH_REUSE_GRACE_SECONDS):
            return False
        alive = select(RefreshToken.id).where(
            RefreshToken.family_id == token.family_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        return (await self._session.execute(alive.limit(1))).first() is not None

    async def _revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session="fetch")
        )

    async def _issue(self, user: User, *, family_id: uuid.UUID) -> AuthResult:
        result, _ = await self._issue_with_row(user, family_id=family_id)
        return result

    async def _issue_with_row(self, user: User, *, family_id: uuid.UUID) -> tuple[AuthResult, RefreshToken]:
        raw = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.refresh_token_ttl_seconds)
        row = RefreshToken(
            user_id=user.id, family_id=family_id, token_hash=_hash_refresh_token(raw), expires_at=expires_at
        )
        self._session.add(row)
        await self._session.flush()
        result = AuthResult(
            user=user,
            access_token=self._tokens.issue(user.id),
            refresh_token=raw,
            refresh_expires_at=expires_at,
        )
        return result, row
