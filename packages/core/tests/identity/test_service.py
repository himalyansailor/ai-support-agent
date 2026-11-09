import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from sqlalchemy import select

from supportcore.config import Settings
from supportcore.db.models import RefreshToken, User
from supportcore.events import EventBus
from supportcore.identity.argon2_hasher import Argon2PasswordHasher
from supportcore.identity.jwt_tokens import JwtAccessTokenService
from supportcore.identity.ports import InvalidTokenError
from supportcore.identity.service import (
    REFRESH_REUSE_GRACE_SECONDS,
    AuthService,
    EmailTakenError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    UserSignedUp,
    WeakPasswordError,
)

SECRET = "test-secret-test-secret-test-secret-123"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        database_url="postgresql+asyncpg://unused/unused",
        redis_url="redis://localhost:6379/0",
        jwt_secret=SECRET,
        smtp_from="noreply@test.local",
    )


class SpyHasher:
    """Wraps the real hasher and records every verify call."""

    def __init__(self) -> None:
        self.inner = Argon2PasswordHasher()
        self.verify_calls = 0

    def hash(self, password: str) -> str:
        return self.inner.hash(password)

    def verify(self, hash: str, password: str) -> bool:
        self.verify_calls += 1
        return self.inner.verify(hash, password)


@pytest.fixture
def hasher() -> SpyHasher:
    return SpyHasher()


@pytest.fixture
def tokens(settings) -> JwtAccessTokenService:
    return JwtAccessTokenService(secret=SECRET, ttl_seconds=settings.access_token_ttl_seconds)


@pytest.fixture
def events() -> EventBus:
    return EventBus()


@pytest.fixture
def auth(session, hasher, tokens, settings, events) -> AuthService:
    return AuthService(session, hasher, tokens, settings, events)


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# --- hasher / tokens -------------------------------------------------------


def test_argon2_hash_and_verify():
    h = Argon2PasswordHasher()
    hashed = h.hash("correct-horse-1")
    assert hashed != "correct-horse-1" and hashed.startswith("$argon2")
    assert h.verify(hashed, "correct-horse-1")
    assert not h.verify(hashed, "wrong-password")
    assert not h.verify("not-a-hash", "correct-horse-1")


def test_access_token_roundtrip(tokens):
    uid = uuid4()
    token = tokens.issue(uid)
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims["sub"] == str(uid) and claims["type"] == "access"
    assert claims["exp"] - claims["iat"] == 900
    assert tokens.decode(token) == uid


def test_access_token_rejects_wrong_type_expired_and_bad_signature(tokens):
    now = datetime.now(UTC)
    base = {"sub": str(uuid4()), "iat": now, "exp": now + timedelta(minutes=5)}
    with pytest.raises(InvalidTokenError):
        tokens.decode(jwt.encode({**base, "type": "refresh"}, SECRET, algorithm="HS256"))
    with pytest.raises(InvalidTokenError):
        tokens.decode(jwt.encode({**base, "type": "access", "exp": now - timedelta(seconds=1)}, SECRET))
    with pytest.raises(InvalidTokenError):
        tokens.decode(jwt.encode({**base, "type": "access"}, "another-secret-another-secret-123"))
    with pytest.raises(InvalidTokenError):
        tokens.decode(jwt.encode({**base, "type": "access", "sub": "not-a-uuid"}, SECRET))
    with pytest.raises(InvalidTokenError):
        tokens.decode("garbage")


# --- signup / login --------------------------------------------------------


async def test_email_normalisation(auth):
    await auth.signup(" Bob@X.com ", "correct-horse-1", "Bob")
    res = await auth.login("bob@x.com", "correct-horse-1")
    assert res.user.email == "bob@x.com"
    with pytest.raises(EmailTakenError):
        await auth.signup("BOB@x.com", "correct-horse-1", "Bob2")


async def test_signup_stores_hash_and_issues_tokens(auth, session, tokens, events):
    seen = []

    async def on_signup(event):
        seen.append(event)

    events.subscribe(UserSignedUp, on_signup)
    res = await auth.signup("a@x.com", "correct-horse-1", "A")
    user = (await session.execute(select(User).where(User.email == "a@x.com"))).scalar_one()
    assert user.password_hash != "correct-horse-1"
    assert tokens.decode(res.access_token) == user.id
    row = (await session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalar_one()
    assert row.token_hash == _sha256(res.refresh_token)  # stored hashed, never raw
    assert row.token_hash != res.refresh_token
    assert row.expires_at == res.refresh_expires_at
    expected = datetime.now(UTC) + timedelta(seconds=2592000)
    assert abs((res.refresh_expires_at - expected).total_seconds()) < 60
    assert [e.user_id for e in seen] == [user.id]
    assert seen[0].actor_user_id == user.id


async def test_login_wrong_password(auth):
    await auth.signup("a@x.com", "correct-horse-1", "A")
    with pytest.raises(InvalidCredentialsError):
        await auth.login("a@x.com", "wrong-horse-1")


async def test_login_unknown_email_same_error(auth, hasher):
    with pytest.raises(InvalidCredentialsError):
        await auth.login("nobody@x.com", "correct-horse-1")
    assert hasher.verify_calls == 1  # dummy verify keeps timing similar to a wrong password


async def test_login_inactive_user_rejected(auth, session):
    r = await auth.signup("a@x.com", "correct-horse-1", "A")
    r.user.is_active = False
    await session.flush()
    with pytest.raises(InvalidCredentialsError):
        await auth.login("a@x.com", "correct-horse-1")


async def test_weak_password_rejected(auth):
    with pytest.raises(WeakPasswordError):
        await auth.signup("a@x.com", "short", "A")
    with pytest.raises(WeakPasswordError):
        await auth.signup("a@x.com", "123456789", "A")  # 9 chars
    await auth.signup("a@x.com", "1234567890", "A")  # 10 chars is enough


# --- refresh rotation ------------------------------------------------------


async def test_refresh_rotates(auth):
    r1 = await auth.signup("a@x.com", "correct-horse-1", "A")
    r2 = await auth.refresh(r1.refresh_token)
    assert r2.refresh_token != r1.refresh_token
    assert r2.user.id == r1.user.id


async def test_refresh_rotation_links_old_to_new(auth, session):
    r1 = await auth.signup("a@x.com", "correct-horse-1", "A")
    r2 = await auth.refresh(r1.refresh_token)
    old = (await session.execute(select(RefreshToken).where(RefreshToken.token_hash == _sha256(r1.refresh_token)))).scalar_one()
    new = (await session.execute(select(RefreshToken).where(RefreshToken.token_hash == _sha256(r2.refresh_token)))).scalar_one()
    assert old.revoked_at is not None
    assert old.replaced_by_id == new.id
    assert new.family_id == old.family_id
    assert new.revoked_at is None


async def _row(session, raw: str) -> RefreshToken:
    stmt = select(RefreshToken).where(RefreshToken.token_hash == _sha256(raw))
    return (await session.execute(stmt)).scalar_one()


async def _age_rotation(session, raw: str, seconds: float) -> None:
    """Pretend the token was rotated `seconds` ago."""
    (await _row(session, raw)).revoked_at = datetime.now(UTC) - timedelta(seconds=seconds)
    await session.flush()


async def test_refresh_reuse_revokes_family(auth, session):
    r1 = await auth.signup("a@x.com", "correct-horse-1", "A")
    r2 = await auth.refresh(r1.refresh_token)
    await _age_rotation(session, r1.refresh_token, REFRESH_REUSE_GRACE_SECONDS + 1)
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh(r1.refresh_token)          # replay outside the grace window
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh(r2.refresh_token)          # family revoked


async def test_refresh_concurrent_reuse_within_grace_issues_sibling(auth, session):
    """Two tabs refresh with the same token: the loser gets a sibling token, the family survives."""
    r1 = await auth.signup("a@x.com", "correct-horse-1", "A")
    r2 = await auth.refresh(r1.refresh_token)            # tab A wins the race
    await _age_rotation(session, r1.refresh_token, REFRESH_REUSE_GRACE_SECONDS - 2)
    r3 = await auth.refresh(r1.refresh_token)            # tab B, a moment later
    assert r3.refresh_token not in (r1.refresh_token, r2.refresh_token)
    assert r3.user.id == r1.user.id
    sibling, winner = await _row(session, r3.refresh_token), await _row(session, r2.refresh_token)
    assert sibling.family_id == winner.family_id
    assert sibling.revoked_at is None and winner.revoked_at is None
    # both tabs keep working
    assert (await auth.refresh(r2.refresh_token)).refresh_token
    assert (await auth.refresh(r3.refresh_token)).refresh_token


async def test_refresh_logout_revoked_token_within_grace_revokes_family(auth, session):
    r1 = await auth.signup("a@x.com", "correct-horse-1", "A")
    other_tab = await auth.refresh(r1.refresh_token)
    await auth.logout(other_tab.refresh_token)           # revoked by logout, not rotation
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh(other_tab.refresh_token)
    # the rotated r1 is inside the grace window, but the family is dead: no resurrection
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh(r1.refresh_token)


async def test_refresh_reuse_does_not_touch_other_families(auth, session):
    a = await auth.signup("a@x.com", "correct-horse-1", "A")
    other_session = await auth.login("a@x.com", "correct-horse-1")  # second device, new family
    await auth.refresh(a.refresh_token)
    await _age_rotation(session, a.refresh_token, REFRESH_REUSE_GRACE_SECONDS + 1)
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh(a.refresh_token)
    assert (await auth.refresh(other_session.refresh_token)).refresh_token


async def test_refresh_unknown_token(auth):
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh("not-a-real-token")


async def test_refresh_expired_token(auth, session):
    r1 = await auth.signup("a@x.com", "correct-horse-1", "A")
    row = (await session.execute(select(RefreshToken).where(RefreshToken.token_hash == _sha256(r1.refresh_token)))).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh(r1.refresh_token)


async def test_refresh_inactive_user(auth, session):
    r1 = await auth.signup("a@x.com", "correct-horse-1", "A")
    r1.user.is_active = False
    await session.flush()
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh(r1.refresh_token)


# --- logout ----------------------------------------------------------------


async def test_logout_revokes_refresh_token(auth):
    r1 = await auth.signup("a@x.com", "correct-horse-1", "A")
    await auth.logout(r1.refresh_token)
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh(r1.refresh_token)


async def test_logout_unknown_token_is_noop(auth):
    await auth.logout("not-a-real-token")
