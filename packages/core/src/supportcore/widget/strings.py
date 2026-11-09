"""Every customer-facing string of the widget, by language. Sent to the iframe in the config
response; the iframe page hard-codes no English. The business may override `greeting`."""

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "greeting": "Hi! How can we help you today?",
        "placeholder": "Type your message...",
        "send": "Send",
        "connecting_human": "Connecting you with a person from our team...",
        "human_offer": "Would you like to talk to a person instead?",
        "talk_to_human": "Talk to a person",
        "no_agent_email_prompt": "Nobody is available right now. Leave your email and we will get back to you.",
        "email_saved": "Thanks, we have your email and will be in touch soon.",
        "bot_error": "Sorry, something went wrong. Please try again.",
        "end_chat": "End chat",
        "csat_prompt": "How was your experience?",
        "csat_thanks": "Thanks for your feedback!",
        "new_conversation": "Start a new conversation",
        "powered_by": "Powered by Support",
        "agent_joined": "{name} has joined the conversation",
        "no_agent_available": "Our team isn't available right now. Leave your email and we'll get back to you.",
        "back_to_bot": "You're chatting with our assistant again.",
        "conversation_resolved": "This conversation has been resolved.",
        "call_button": "Call us",
        "call_connecting": "Connecting\u2026",
        "call_in_progress": "On a call",
        "call_end": "End call",
        "call_mute": "Mute",
        "call_unmute": "Unmute",
        "call_ended": "Call ended",
        "call_mic_denied": "Allow microphone access to call us.",
        "call_failed": "The call could not be connected.",
        "call_dismiss": "Dismiss",
    },
}

DEFAULT_LANGUAGE = "en"


def strings_for(language: str | None) -> dict[str, str]:
    """A copy of the strings for `language`, falling back to English."""
    return dict(STRINGS.get(language or DEFAULT_LANGUAGE, STRINGS[DEFAULT_LANGUAGE]))
