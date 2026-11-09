import random

import pytest
from cryptography.fernet import Fernet

from supportcore.config import Settings
from supportcore.errors import DomainError
from supportcore.knowledge.chunking.ports import ChunkingParams, InvalidChunkingParamsError
from supportcore.knowledge.parsing.ports import PageSpan, ParsedDocument
from supportcore.wiring import build_adapters, chunker_registry

STRATEGIES = ("fixed", "recursive", "markdown")


def _chunk(strategy, doc, size=200, overlap=40):
    return chunker_registry.create(strategy).chunk(
        doc, ChunkingParams(strategy=strategy, chunk_size=size, chunk_overlap=overlap)
    )


def _random_paragraphs(rng: random.Random, n: int) -> str:
    words = ["refund", "order", "ship", "return", "policy", "the", "a", "within", "days", "item"]
    paras = [" ".join(rng.choice(words) for _ in range(rng.randint(5, 80))) + "." for _ in range(n)]
    return "\n\n".join(paras)


def _markdown_with_repeats(rng: random.Random) -> str:
    parts = []
    for i in range(6):
        parts.append(f"# Returns\n\n## Refunds\n\nSame line.\nSame line.\n{_random_paragraphs(rng, 3)}")
        parts.append(f"## Shipping {i % 2}\n\nSame line.\n\n### Details\n\n{_random_paragraphs(rng, 2)}")
    return "Intro text before headings.\n\n" + "\n\n".join(parts)


def test_params_defaults_and_validation():
    p = ChunkingParams(strategy="recursive")
    assert (p.chunk_size, p.chunk_overlap) == (1000, 150)
    assert issubclass(InvalidChunkingParamsError, DomainError)
    for kwargs in (
        {"strategy": "fixed", "chunk_size": 400, "chunk_overlap": 201},
        {"strategy": "fixed", "chunk_size": 50},
        {"strategy": "fixed", "chunk_size": 8001},
        {"strategy": "fixed", "chunk_overlap": -1},
        {"strategy": "semantic"},
    ):
        with pytest.raises(InvalidChunkingParamsError) as ei:
            ChunkingParams(**kwargs)
        assert "pydantic" not in str(ei.value).lower() and "\n" not in str(ei.value)
    with pytest.raises(InvalidChunkingParamsError):
        ChunkingParams.model_validate({"strategy": "fixed", "chunk_size": 50})
    ChunkingParams(strategy="fixed", chunk_size=400, chunk_overlap=200)


def test_fixed_respects_size_and_overlap():
    doc = ParsedDocument(text="abcdefghij" * 100)
    chunks = _chunk("fixed", doc, size=100, overlap=20)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(len(c.text) <= 100 for c in chunks)
    assert len(chunks[0].text) == 100
    for a, b in zip(chunks, chunks[1:]):
        assert b.char_start == a.char_end - 20


def test_recursive_prefers_paragraph_boundaries():
    p1, p2 = "a " * 60, "b " * 60
    doc = ParsedDocument(text=f"{p1.strip()}\n\n{p2.strip()}")
    chunks = _chunk("recursive", doc, size=150, overlap=0)
    assert [c.text for c in chunks] == [p1.strip(), p2.strip()]


def test_markdown_carries_headings_metadata():
    text = "Preface.\n\n# Returns\n\nIntro.\n\n## Refunds\n\nRefund within 30 days.\n\n## Exchanges\n\nSwap it."
    chunks = _chunk("markdown", ParsedDocument(text=text), size=500, overlap=0)
    refunds = next(c for c in chunks if "Refund within" in c.text)
    assert refunds.metadata["headings"] == ["Returns", "Refunds"]
    assert refunds.text.startswith("## Refunds")
    exch = next(c for c in chunks if "Swap it" in c.text)
    assert exch.metadata["headings"] == ["Returns", "Exchanges"]
    pre = next(c for c in chunks if c.text.startswith("Preface"))
    assert pre.metadata.get("headings", []) == []


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("seed", range(5))
def test_char_spans_match_source(strategy, seed):
    rng = random.Random(seed)
    text = _markdown_with_repeats(rng) if strategy == "markdown" or seed % 2 else _random_paragraphs(rng, 12)
    text = "\n\n  " + text + "  \n"  # leading/trailing whitespace the splitters strip
    for size, overlap in ((100, 0), (200, 40), (300, 150), (1000, 150)):
        chunks = _chunk(strategy, ParsedDocument(text=text), size=size, overlap=overlap)
        assert chunks
        for i, c in enumerate(chunks):
            assert c.ordinal == i
            assert text[c.char_start : c.char_end] == c.text
            assert c.text.strip()
            assert len(c.text) <= size
        starts = [c.char_start for c in chunks]
        assert starts == sorted(starts)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_whitespace_only_document_yields_no_chunks(strategy):
    assert _chunk(strategy, ParsedDocument(text="  \n\n \t ")) == []


def test_pages_attached_from_pdf_spans():
    p1, p2 = "First page text. " * 10, "Second page text. " * 10
    text = p1 + p2
    pages = [PageSpan(1, 0, len(p1)), PageSpan(2, len(p1), len(text))]
    chunks = _chunk("fixed", ParsedDocument(text=text, pages=pages), size=100, overlap=0)
    for c in chunks:
        covered = [p.page for p in pages if c.char_start < p.char_end and c.char_end > p.char_start]
        if len(covered) == 1:
            assert c.metadata["page"] == covered[0] and "pages" not in c.metadata
        else:
            assert c.metadata["pages"] == covered and "page" not in c.metadata
    assert chunks[0].metadata["page"] == 1
    assert chunks[-1].metadata["page"] == 2
    assert any("pages" in c.metadata for c in chunks)


def test_no_page_keys_without_pages():
    for c in _chunk("recursive", ParsedDocument(text="hello world " * 50)):
        assert "page" not in c.metadata and "pages" not in c.metadata


def test_adapters_expose_chunkers():
    settings = Settings(
        env="test",
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(),
    )
    adapters = build_adapters(settings)
    assert set(adapters.chunkers) == set(STRATEGIES)


def test_markdown_sections_are_anchored_to_real_headings():
    text = (
        "### X\n\nDeep x.\n\n"
        "## X\n\nMid x.\n\n"
        "## Y\n\nMid y.\n\n"
        "# X\n\nTop x.\n\n"
        "## B\n\n```\n## B\nnot a heading\n```\n\nafter fence\n\n"
        "## B\n\nReal second b."
    )
    chunks = _chunk("markdown", ParsedDocument(text=text), size=500, overlap=0)
    for c in chunks:
        assert c.char_start == 0 or text[c.char_start - 1] == "\n"
        assert c.text.strip("# \n") != ""
        assert text[c.char_start : c.char_end] == c.text
    got = [(c.text.split("\n", 1)[0], c.metadata["headings"]) for c in chunks]
    assert got == [
        ("### X", ["X"]),
        ("## X", ["X"]),
        ("## Y", ["Y"]),
        ("# X", ["X"]),
        ("## B", ["X", "B"]),
        ("## B", ["X", "B"]),
    ]
    fenced = chunks[4]
    assert "```\n## B\nnot a heading\n```" in fenced.text and "after fence" in fenced.text
    assert chunks[5].text == "## B\n\nReal second b."
