"""Strings of the emails the platform writes itself, by language (keyed like widget/strings.py)."""

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "email_followup_subject": "Your support request",
        "email_truncated": "[Message truncated]",
    },
}

DEFAULT_LANGUAGE = "en"


def strings_for(language: str | None) -> dict[str, str]:
    return STRINGS.get((language or DEFAULT_LANGUAGE).split("-")[0].lower(), STRINGS[DEFAULT_LANGUAGE])
