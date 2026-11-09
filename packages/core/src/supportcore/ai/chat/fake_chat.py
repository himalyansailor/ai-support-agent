"""Offline chat model for tests (registered only when settings.env == "test")."""

from typing import TYPE_CHECKING

from supportcore.ai.catalog import get_chat_spec

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

FAKE_ANSWER = "Fake answer [1]"


def fake_chat_model(
    *,
    model: str,
    api_key: str | None,
    base_url: str | None,
    temperature: float,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> "BaseChatModel":
    get_chat_spec("fake", model, include_fake=True)
    from langchain_core.language_models import FakeListChatModel

    return FakeListChatModel(responses=[FAKE_ANSWER])
