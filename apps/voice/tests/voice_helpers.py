import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from supportcore.db.models import (
    Organisation,
    PhoneNumber,
    TelephonyAccount,
    VoiceSettings,
    Widget,
)
from supportcore.voice.telephony.signature import twilio_signature

PUBLIC_URL = "https://voice.example.com"
ACCOUNT_SID = "AC" + "a" * 32
AUTH_TOKEN = "t" * 32
NUMBER_E164 = "+15005550006"
CALLER_E164 = "+14155550123"


class RecordingQueue:
    """Stands in for the job queue: records what was enqueued."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict]] = []

    def enqueue(self, name: str, payload: dict, *, countdown: float | None = None) -> None:
        self.jobs.append((name, payload))


@dataclass
class Seed:
    organisation_id: uuid.UUID
    widget_id: uuid.UUID
    number_id: uuid.UUID
    account_id: uuid.UUID


async def seed_org(app, session_factory, *, ready=True, browser=True, suffix="a", e164=NUMBER_E164, sid=ACCOUNT_SID) -> Seed:
    cipher = app.state.container.adapters.cipher
    async with session_factory() as s:
        org = Organisation(name=f"Acme {suffix}", slug=f"acme-{suffix}")
        s.add(org)
        await s.flush()
        widget = Widget(organisation_id=org.id, name="W", public_key="pk-" + uuid.uuid4().hex)
        account = TelephonyAccount(
            organisation_id=org.id,
            kind="byo",
            account_sid=sid,
            auth_token_encrypted=cipher.encrypt(AUTH_TOKEN),
            auth_token_last4=AUTH_TOKEN[-4:],
            api_key_sid="SK" + "b" * 32,
            api_key_secret_encrypted=cipher.encrypt("secret"),
            twiml_app_sid="AP" + "c" * 32,
        )
        s.add_all([widget, account])
        await s.flush()
        number = PhoneNumber(
            organisation_id=org.id,
            telephony_account_id=account.id,
            e164=e164,
            provider_sid="PN" + "d" * 32,
            ownership="byo",
            widget_id=widget.id,
            webhook_url=f"{PUBLIC_URL}/twilio/voice/inbound",
        )
        voice = VoiceSettings(
            organisation_id=org.id,
            phone_enabled=True,
            browser_calls_enabled=browser,
            **(
                {"stt_provider": "fake", "stt_model": "fake-stt", "tts_provider": "fake", "tts_model": "fake-tts", "tts_voice": "fake"}
                if ready
                else {}
            ),
        )
        s.add_all([number, voice])
        await s.commit()
        return Seed(org.id, widget.id, number.id, account.id)


def signed_headers(url_path: str, params: dict[str, str], *, token: str = AUTH_TOKEN, query: str = "") -> dict[str, str]:
    url = PUBLIC_URL + url_path + (f"?{query}" if query else "")
    return {"X-Twilio-Signature": twilio_signature(token, url, params)}


async def post_signed(client, path: str, params: dict[str, str], *, token: str = AUTH_TOKEN, query: str = ""):
    target = path + (f"?{query}" if query else "")
    return await client.post(target, data=params, headers=signed_headers(path, params, token=token, query=query))


def path_of(url: str) -> str:
    return urlsplit(url).path
