import uuid

import pytest
from cryptography.fernet import Fernet
from supportcore.audit.subscribers import register_audit_subscribers
from supportcore.billing.dev_billing import DevBilling
from supportcore.billing.memory_counter import MemoryUsageCounter
from supportcore.config import Settings
from supportcore.db.models import User, Widget
from supportcore.db.session import use_current_session
from supportcore.events import EventBus
from supportcore.secrets.fernet_cipher import FernetCipher
from supportcore.voice.telephony.fake_telephony import FakeTelephony

PUBLIC_URL = "https://voice.example.com"
PLATFORM_SID = "AC" + "a" * 32
BYO_SID = "AC" + "b" * 32
BYO_TOKEN = "t" * 32
BAD_TOKEN = "bad-token" + "0" * 23


class Adapters:
    def __init__(self) -> None:
        self.cipher = FernetCipher(Fernet.generate_key().decode())
        self.telephony = FakeTelephony()
        self.usage_counter = MemoryUsageCounter()
        self.billing = DevBilling()  # no limits; a test sets FakeBilling() to enforce a plan
        self.credential_validator = None  # only CredentialService.create uses it


@pytest.fixture
def adapters():
    return Adapters()


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        env="test",
        jwt_secret="test-secret-test-secret-test-secret-123",
        voice_public_url=PUBLIC_URL,
        twilio_account_sid=PLATFORM_SID,
        twilio_auth_token="p" * 32,
    )


@pytest.fixture
def settings_no_platform(settings):
    return settings.model_copy(update={"twilio_account_sid": None, "twilio_auth_token": None})


@pytest.fixture
async def owner(session):
    u = User(email="owner@x.com", password_hash="x", name="Owner")
    session.add(u)
    await session.flush()
    return u


@pytest.fixture
async def org(session, owner):
    from supportcore.orgs.service import OrganisationService

    return await OrganisationService(session, EventBus()).create(owner, "Acme")


@pytest.fixture
async def widget(session, org):
    w = Widget(organisation_id=org.id, name="W", public_key="pk-" + uuid.uuid4().hex)
    session.add(w)
    await session.flush()
    return w


@pytest.fixture
def bus(session):
    b = EventBus()
    register_audit_subscribers(b, lambda: session)
    return b


@pytest.fixture(autouse=True)
def _current_session(session):
    with use_current_session(session):
        yield
