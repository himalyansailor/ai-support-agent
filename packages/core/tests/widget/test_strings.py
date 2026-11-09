from supportcore.widget.strings import STRINGS, strings_for

KEYS = {
    "greeting", "placeholder", "send", "connecting_human", "human_offer", "talk_to_human",
    "no_agent_email_prompt", "email_saved", "bot_error", "end_chat", "csat_prompt", "csat_thanks",
    "new_conversation", "powered_by", "agent_joined", "no_agent_available", "back_to_bot", "conversation_resolved",
    "call_button", "call_connecting", "call_in_progress", "call_end", "call_mute", "call_unmute", "call_ended",
    "call_mic_denied", "call_failed", "call_dismiss",
}


def test_en_has_all_keys():
    assert set(STRINGS["en"]) == KEYS
    assert all(STRINGS["en"].values())


def test_strings_for_falls_back_to_en():
    assert strings_for("fr") == STRINGS["en"]
    assert strings_for("en") == STRINGS["en"]
    assert strings_for("xx") is not STRINGS["en"]  # a copy, safe to mutate


def test_phase_4_strings():
    en = STRINGS["en"]
    assert en["agent_joined"].format(name="Ann") == "Ann has joined the conversation"
    assert en["no_agent_available"] == "Our team isn't available right now. Leave your email and we'll get back to you."
