"""TwiML builders. Every value is XML-escaped; nothing here logs."""

from uuid import UUID
from xml.sax.saxutils import escape, quoteattr

__all__ = [
    "HOLD_TRACK_URL",
    "agent_conference",
    "conference_room",
    "connect_stream",
    "dial_customer",
    "gather_accept",
    "hangup",
    "hold_pause",
    "join_conference",
    "play_hold",
    "say_and_hangup",
]

# Twilio's own default wait music (Assumption 6).
HOLD_TRACK_URL = "https://com.twilio.music.classical.s3.amazonaws.com/BusyStrings.mp3"

# Twilio's <Say> wants a locale; unknown languages are passed through unchanged.
_SAY_LOCALES = {
    "en": "en-US",
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "pt": "pt-BR",
    "nl": "nl-NL",
    "ja": "ja-JP",
    "ru": "ru-RU",
    "hi": "hi-IN",
}


def _response(*elements: str) -> str:
    return "<Response>" + "".join(elements) + "</Response>"


def _say(text: str, language: str) -> str:
    locale = _SAY_LOCALES.get(language.split("-")[0].lower(), language)
    return f"<Say language={quoteattr(locale)}>{escape(text)}</Say>"


def connect_stream(stream_url: str, token: str, action_url: str) -> str:
    return _response(
        f"<Connect action={quoteattr(action_url)}>"
        f"<Stream url={quoteattr(stream_url)}>"
        f'<Parameter name="token" value={quoteattr(token)}/>'
        "</Stream></Connect>"
    )


def say_and_hangup(text: str, language: str) -> str:
    return _response(_say(text, language), "<Hangup/>")


def hangup() -> str:
    return _response("<Hangup/>")


def conference_room(transfer_id: UUID) -> str:
    return f"transfer-{transfer_id}"


def join_conference(transfer_id: UUID, hold_url: str, status_callback_url: str) -> str:
    """The caller waits in the transfer's room (hearing `hold_url`) until an agent starts it. The caller joins
    first, and Twilio takes the room's status callback from the first participant, so it is set here: participants
    joining are reported to `status_callback_url` (an agent leg that drops before it gets in is noticed)."""
    return _response(
        "<Dial><Conference"
        ' startConferenceOnEnter="false" endConferenceOnExit="true" beep="false"'
        f' waitUrl={quoteattr(hold_url)} waitMethod="POST"'
        f' statusCallback={quoteattr(status_callback_url)} statusCallbackEvent="join" statusCallbackMethod="POST">'
        f"{escape(conference_room(transfer_id))}</Conference></Dial>"
    )


def agent_conference(transfer_id: UUID) -> str:
    """The agent enters the transfer's room and starts it."""
    return _response(
        "<Dial><Conference"
        ' startConferenceOnEnter="true" endConferenceOnExit="true" beep="false">'
        f"{escape(conference_room(transfer_id))}</Conference></Dial>"
    )


def gather_accept(text: str, language: str, action_url: str, timeout_seconds: int) -> str:
    """A phone leg: "press 1 to accept"; no key press hangs up (voicemail never accepts)."""
    return _response(
        f'<Gather numDigits="1" timeout="{int(timeout_seconds)}" action={quoteattr(action_url)} method="POST">'
        f"{_say(text, language)}</Gather>",
        "<Hangup/>",
    )


def hold_pause() -> str:
    """A `waitUrl` with nothing to play (its transfer is gone): a waitUrl may only Play, Say, Pause or Redirect."""
    return _response('<Pause length="5"/>')


def play_hold() -> str:
    return _response(f'<Play loop="0">{escape(HOLD_TRACK_URL)}</Play>')


def dial_customer(
    customer_e164: str,
    caller_id: str,
    action_url: str,
    *,
    recording_status_url: str | None = None,
    say: str | None = None,
    language: str = "en",
) -> str:
    """An agent calls a customer back: rings `customer_e164` for up to 30 s from the organisation's number, bridging
    the agent once the customer answers. `action_url` hears the outcome (`DialCallStatus`). With
    `recording_status_url` the call is recorded from the moment it is answered (both sides). `say` is spoken to the
    agent first (their phone just answered)."""
    record = (
        ' record="record-from-answer-dual"'
        f" recordingStatusCallback={quoteattr(recording_status_url)}"
        ' recordingStatusCallbackEvent="completed"'
        if recording_status_url
        else ""
    )
    dial = (
        f'<Dial callerId={quoteattr(caller_id)} answerOnBridge="true" timeout="30"'
        f' action={quoteattr(action_url)} method="POST"{record}>'
        f"<Number>{escape(customer_e164)}</Number></Dial>"
    )
    return _response(*([_say(say, language)] if say else []), dial)
