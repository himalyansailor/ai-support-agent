import math
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest

from supportcore.ai.embeddings.fake_embedder import FakeEmbedder
from supportcore.ai.ports import EmbeddingError
from supportcore.config import Settings
from supportcore.registry import UnknownProviderError
from supportcore.wiring import build_adapters


def _settings(env="test", **overrides):
    return Settings(
        _env_file=None,
        env=env,
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://h:6379/0",
        jwt_secret="x" * 32,
        smtp_from="noreply@example.com",
        **overrides,
    )


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_fake_embedder_deterministic_and_normalised():
    e = FakeEmbedder()
    a, b = await e.embed_documents(["Hello world", "hello   WORLD"])
    assert a == b
    assert len(a) == e.dimensions == 8
    assert math.isclose(_dot(a, a), 1.0, abs_tol=1e-9)
    assert await e.embed_query("hello world") == a
    assert len(e.calls) == 2


async def test_fake_embedder_similar_texts_closer():
    e = FakeEmbedder(dimensions=256)
    q, near, far = await e.embed_documents(
        ["reset your password quickly", "how to reset password", "shipping times for parcels"]
    )
    assert _dot(q, near) > _dot(q, far)


async def test_fake_embedder_fail_on():
    e = FakeEmbedder(fail_on="boom")
    with pytest.raises(EmbeddingError):
        await e.embed_documents(["fine", "a boom here"])
    assert await e.embed_query("fine")


def test_factory_unknown_provider_lists_available():
    factory = build_adapters(_settings()).embedders
    assert factory.providers() == ["openai", "openai_compatible", "local", "fake"]
    with pytest.raises(UnknownProviderError) as exc:
        factory.create("nope", "m", api_key=None, base_url=None, settings=_settings())
    assert "available: openai, openai_compatible, local, fake" in str(exc.value)


def test_fake_provider_only_in_test_env():
    assert "fake" not in build_adapters(_settings("dev")).embedders.providers()


def test_factory_local_rejects_key_and_url():
    s = _settings()
    factory = build_adapters(s).embedders
    with pytest.raises(ValueError):
        factory.create("local", "BAAI/bge-small-en-v1.5", api_key="k", base_url=None, settings=s)
    with pytest.raises(ValueError):
        factory.create("local", "BAAI/bge-small-en-v1.5", api_key=None, base_url="http://x", settings=s)
    assert factory.create("local", "BAAI/bge-small-en-v1.5", api_key=None, base_url=None, settings=s).dimensions == 384


def test_factory_fake_uses_catalog_dimensions():
    s = _settings()
    e = build_adapters(s).embedders.create("fake", "fake-embedding", api_key=None, base_url=None, settings=s)
    assert e.dimensions == 8


async def test_openai_embedder_batches_requests():
    from langchain_openai import OpenAIEmbeddings

    s = _settings(embed_batch_size=64)
    e = build_adapters(s).embedders.create(
        "openai", "text-embedding-3-small", api_key="sk-test-key-1234", base_url=None, settings=s
    )
    assert e.dimensions == 1536

    with patch.object(OpenAIEmbeddings, "aembed_documents", AsyncMock(side_effect=lambda t: [[0.0] for _ in t])) as m:
        out = await e.embed_documents([f"t{i}" for i in range(130)])
    assert [len(c.args[0]) for c in m.call_args_list] == [64, 64, 2]
    assert len(out) == 130


async def test_openai_compatible_dimensions_need_probe():
    from langchain_openai import OpenAIEmbeddings

    s = _settings()
    e = build_adapters(s).embedders.create(
        "openai_compatible", "my-model", api_key="k-123456789", base_url="http://localhost:1/v1", settings=s
    )
    with pytest.raises(EmbeddingError):
        _ = e.dimensions
    with patch.object(OpenAIEmbeddings, "aembed_query", AsyncMock(return_value=[0.1] * 5)):
        assert await e.probe_dimensions() == 5
    assert e.dimensions == 5


async def test_openai_error_is_sanitised():
    from langchain_openai import OpenAIEmbeddings

    key = "sk-supersecretkey-ABCDEFGHIJ"
    s = _settings()
    e = build_adapters(s).embedders.create("openai", "text-embedding-3-small", api_key=key, base_url=None, settings=s)
    boom = RuntimeError(f"Incorrect API key provided: {key}")
    with patch.object(OpenAIEmbeddings, "aembed_query", AsyncMock(side_effect=boom)):
        with pytest.raises(EmbeddingError) as exc:
            await e.embed_query("x")
    assert key not in str(exc.value)
    assert "Incorrect API key" in str(exc.value)


async def test_e5_prefixes_applied(monkeypatch):
    from supportcore.ai.embeddings import local_embedder

    seen = []

    class FakeModel:
        def encode(self, texts, **kwargs):
            seen.append((list(texts), kwargs))
            return [[0.5, 0.5] for _ in texts]

    monkeypatch.setattr(local_embedder, "_get_model", lambda model, cache_dir: FakeModel())
    s = _settings()
    e = build_adapters(s).embedders.create(
        "local", "intfloat/multilingual-e5-small", api_key=None, base_url=None, settings=s
    )
    await e.embed_documents(["a", "b"])
    await e.embed_query("q")
    assert seen[0][0] == ["passage: a", "passage: b"]
    assert seen[1][0] == ["query: q"]
    assert seen[0][1]["normalize_embeddings"] is True

    seen.clear()
    bge = build_adapters(s).embedders.create("local", "BAAI/bge-small-en-v1.5", api_key=None, base_url=None, settings=s)
    await bge.embed_query("q")
    assert seen[0][0] == ["q"]


def test_wiring_import_does_not_load_heavy_libs():
    code = (
        "import sys, supportcore.wiring;"
        "bad=[m for m in ('torch','sentence_transformers','langchain_openai','langchain_anthropic','anthropic','openai','boto3','botocore','pypdf','docx','bs4','langchain_text_splitters','chromadb','pgvector') if m in sys.modules];"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.models
async def test_local_bge_small_real_embedding():
    s = _settings()
    e = build_adapters(s).embedders.create("local", "BAAI/bge-small-en-v1.5", api_key=None, base_url=None, settings=s)
    docs = await e.embed_documents(["How do I reset my password?", "Parcel delivery takes 3 days"])
    q = await e.embed_query("password reset")
    assert e.dimensions == 384
    assert len(docs[0]) == 384
    assert _dot(q, docs[0]) > _dot(q, docs[1])
