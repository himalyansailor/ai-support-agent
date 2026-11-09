"""Everything the voice bot says or Twilio reads out, by language (keyed like widget/strings.py)."""

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "voice_greeting": "Hello, you've reached {organisation}. How can I help you today?",
        "voice_recording_notice": "This call may be recorded.",
        "voice_callback_phone": (
            "I'm passing this to our team. Someone will call you back on this number as soon as they can. Goodbye."
        ),
        "voice_callback_browser": "I'm passing this to our team. Someone will follow up with you in the chat. Goodbye.",
        "voice_still_there": "Are you still there?",
        "voice_goodbye": "Thanks for calling. Goodbye.",
        "voice_max_duration": (
            "We've reached the time limit for this call. Someone from our team will follow up. Goodbye."
        ),
        # The unavailable / trouble messages come in three forms, by who we can reach (see unavailable_key): a phone
        # caller whose number we have, a browser caller (followed up in the chat), a phone caller with no number.
        "voice_unavailable": "Sorry, we can't take your call right now. Someone from our team will call you back. Goodbye.",
        "voice_unavailable_browser": (
            "Sorry, we can't take your call right now. Someone from our team will follow up with you in the chat. Goodbye."
        ),
        "voice_unavailable_unknown": "Sorry, we can't take your call right now. Someone from our team will follow up. Goodbye.",
        "voice_trouble": "Sorry, I'm having trouble right now. Someone from our team will call you back. Goodbye.",
        "voice_trouble_browser": (
            "Sorry, I'm having trouble right now. Someone from our team will follow up with you in the chat. Goodbye."
        ),
        "voice_trouble_unknown": "Sorry, I'm having trouble right now. Someone from our team will follow up. Goodbye.",
        "voice_transferring": "Please hold while I connect you to someone from our team.",
        "voice_transfer_failed_phone": (
            "Sorry, no one could take your call just now. Someone will call you back on this number as soon as they "
            "can. Goodbye."
        ),
        "voice_transfer_failed_browser": (
            "Sorry, no one could take your call just now. Someone will follow up with you in the chat. Goodbye."
        ),
        "voice_transfer_failed_unknown": (
            "Sorry, no one could take your call just now. Someone from our team will follow up. Goodbye."
        ),
        "voice_agent_whisper": "Call from a customer of {organisation}. Press 1 to accept.",
        "voice_agent_taken": "This call has already been answered. Goodbye.",
        "voice_callback_connecting": "Connecting you to the customer.",
        "voice_turn_failed": "Sorry, I couldn't find an answer just now. Could you say that again?",
    },
}

DEFAULT_LANGUAGE = "en"


def strings_for(language: str | None) -> dict[str, str]:
    """A copy of the strings for `language`, falling back to English."""
    return dict(STRINGS.get((language or DEFAULT_LANGUAGE).split("-")[0].lower(), STRINGS[DEFAULT_LANGUAGE]))


# Bases whose call-you-back form has its own `_phone` suffix (the older bases use the bare key for it).
_PHONE_SUFFIXED = frozenset({"voice_transfer_failed"})


def unavailable_key(base: str, source: str, caller_e164: str | None) -> str:
    """`voice_unavailable` / `voice_trouble` / `voice_transfer_failed` (`base`) in the form that promises only what
    we can do: call a phone caller back on their number, follow a browser caller up in the chat, else just
    "follow up"."""
    if source == "browser":
        return f"{base}_browser"
    if not caller_e164:
        return f"{base}_unknown"
    return f"{base}_phone" if base in _PHONE_SUFFIXED else base
