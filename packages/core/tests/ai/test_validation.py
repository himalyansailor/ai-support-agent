import httpx
import pytest
from supportcore.ai.catalog import (
    CREDENTIAL_PROVIDERS,
    chat_models,
    embedding_models_for,
)
from supportcore.ai.errors import CredentialInvalidError
from supportcore.ai.validation import ProviderCredentialValidator
from supportcore.config import Settings
from supportcore.wiring import build_adapters

pytestmark = pytest.mark.asyncio


def _validator(handler):
    settings = Settings(_env_file=None, env="test")
    adapters = build_adapters(settings)
    return ProviderCredentialValidator(
        adapters.embedders, adapters.chat_models, settings=settings, http_transport=httpx.MockTransport(handler)
    )


async def test_deepgram_key_validated_via_projects_endpoint():
    seen = []

    def ok(request):
        seen.append(request)
        return httpx.Response(200, json={"projects": []})

    await _validator(ok).validate("deepgram", "dg-key", None)
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "https://api.deepgram.com/v1/projects"
    assert seen[0].headers["Authorization"] == "Token dg-key"


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "The provider rejected this API key"),
        (403, "The provider rejected this API key"),
        (500, "Could not validate the API key with the provider"),
        (204, "Could not validate the API key with the provider"),
    ],
)
async def test_deepgram_rejections(status, message):
    with pytest.raises(CredentialInvalidError) as err:
        await _validator(lambda r: httpx.Response(status, text="body dg-key")).validate("deepgram", "dg-key", None)
    assert str(err.value) == message


async def test_deepgram_not_a_chat_provider():
    assert "deepgram" in CREDENTIAL_PROVIDERS
    assert "deepgram" not in {m.provider for m in chat_models(include_fake=True)}
    assert "deepgram" not in {m.provider for m in embedding_models_for("en", include_fake=True)}
