import pytest

from supportcore.ai.catalog import (
    LANGUAGES,
    UnknownModelError,
    chat_models,
    embedding_models_for,
    get_chat_spec,
    get_embedding_spec,
)


def test_catalog_recommended_chat_model():
    recommended = [m for m in chat_models() if m.recommended]
    assert len(recommended) == 1
    assert (recommended[0].provider, recommended[0].model) == ("anthropic", "claude-sonnet-5-5")


def test_chat_models_free_text_for_openai_and_compatible():
    by_provider = {(m.provider, m.model) for m in chat_models()}
    assert ("openai", "*") in by_provider
    assert ("openai_compatible", "*") in by_provider
    assert {m.provider for m in chat_models()} == {"anthropic", "openai", "openai_compatible"}
    assert all(m.requires_credential for m in chat_models())


def test_embedding_models_for_en_includes_bge_small_and_openai():
    specs = {(m.provider, m.model): m for m in embedding_models_for("en")}
    bge = specs[("local", "BAAI/bge-small-en-v1.5")]
    assert (bge.dimensions, bge.recommended, bge.requires_credential) == (384, True, False)
    assert specs[("local", "intfloat/multilingual-e5-small")].dimensions == 384
    assert specs[("openai", "text-embedding-3-small")].dimensions == 1536
    assert specs[("openai", "text-embedding-3-large")].dimensions == 3072
    assert specs[("openai", "text-embedding-3-small")].requires_credential
    compat = specs[("openai_compatible", "*")]
    assert compat.dimensions is None and compat.requires_credential
    assert [m for m in embedding_models_for("en") if m.recommended] == [bge]


def test_embedding_models_filtered_by_language():
    models = {(m.provider, m.model) for m in embedding_models_for("de")}
    assert ("local", "BAAI/bge-small-en-v1.5") not in models
    assert ("local", "intfloat/multilingual-e5-small") in models
    assert ("openai", "text-embedding-3-small") in models


def test_fake_provider_hidden_by_default():
    assert all(m.provider != "fake" for m in chat_models())
    assert all(m.provider != "fake" for m in embedding_models_for("en"))
    with pytest.raises(UnknownModelError):
        get_embedding_spec("fake", "fake-embedding")

    [fake_chat] = [m for m in chat_models(include_fake=True) if m.provider == "fake"]
    assert fake_chat.model == "fake-chat"
    [fake_emb] = [
        m for m in embedding_models_for("en", include_fake=True) if m.provider == "fake" and m.model == "fake-embedding"
    ]
    assert (fake_emb.model, fake_emb.dimensions) == ("fake-embedding", 8)
    assert get_embedding_spec("fake", "fake-embedding", include_fake=True) == fake_emb


def test_get_embedding_spec_resolves_free_text_and_rejects_unknown():
    spec = get_embedding_spec("openai_compatible", "nomic-embed-text")
    assert spec.model == "*"
    assert get_embedding_spec("openai", "text-embedding-3-large").dimensions == 3072
    with pytest.raises(UnknownModelError):
        get_embedding_spec("openai", "text-embedding-9")
    with pytest.raises(UnknownModelError):
        get_embedding_spec("nope", "x")


def test_get_chat_spec():
    assert get_chat_spec("openai", "gpt-anything").model == "*"
    assert get_chat_spec("anthropic", "claude-sonnet-5-5").recommended
    with pytest.raises(UnknownModelError):
        get_chat_spec("anthropic", "claude-unknown")
    with pytest.raises(UnknownModelError):
        get_chat_spec("openai", "  ")


def test_languages():
    assert LANGUAGES == [("en", "English")]
