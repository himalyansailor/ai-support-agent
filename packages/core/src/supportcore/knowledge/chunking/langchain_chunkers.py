"""LangChain-backed chunkers. Splitter output is only used to find boundaries: spans are located in
the source text and `text` is always the exact source slice, so the chunk preview can trust them."""

import re
from typing import TYPE_CHECKING

from supportcore.knowledge.chunking.ports import ChunkDraft, ChunkingParams
from supportcore.knowledge.parsing.ports import PageSpan, ParsedDocument

if TYPE_CHECKING:
    from langchain_text_splitters import RecursiveCharacterTextSplitter


def _locate(text: str, pieces: list[str], overlap: int, base: int = 0) -> list[tuple[int, int]]:
    """Spans of `pieces` in `text` (offset by `base`). A chunk starts after the previous chunk's start
    and no earlier than `overlap` chars before the previous chunk's end, so the search resumes there;
    that keeps repeated identical text from matching an earlier occurrence."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for piece in pieces:
        start = text.find(piece, cursor)
        if start < 0:
            raise ValueError("Splitter produced text that is not in the source document")
        end = start + len(piece)
        spans.append((base + start, base + end))
        cursor = max(start + 1, end - overlap)
    return spans


def _pages_metadata(pages: list[PageSpan], start: int, end: int) -> dict:
    covered = [p.page for p in pages if p.char_start < end and p.char_end > start]
    if not covered:
        return {}
    return {"page": covered[0]} if len(covered) == 1 else {"pages": covered}


def _finish(doc: ParsedDocument, spans: list[tuple[int, int, dict]]) -> list[ChunkDraft]:
    drafts = []
    for start, end, extra in spans:
        if not doc.text[start:end].strip():
            continue
        metadata = {**_pages_metadata(doc.pages, start, end), **extra}
        drafts.append(ChunkDraft(len(drafts), doc.text[start:end], start, end, metadata))
    return drafts


class FixedChunker:
    def chunk(self, doc: ParsedDocument, params: ChunkingParams) -> list[ChunkDraft]:
        from langchain_text_splitters import CharacterTextSplitter

        splitter = CharacterTextSplitter(
            separator="", chunk_size=params.chunk_size, chunk_overlap=params.chunk_overlap, add_start_index=True
        )
        pieces = splitter.split_text(doc.text)
        return _finish(doc, [(s, e, {}) for s, e in _locate(doc.text, pieces, params.chunk_overlap)])


class RecursiveChunker:
    def chunk(self, doc: ParsedDocument, params: ChunkingParams) -> list[ChunkDraft]:
        splitter = _recursive(params)
        pieces = splitter.split_text(doc.text)
        return _finish(doc, [(s, e, {}) for s, e in _locate(doc.text, pieces, params.chunk_overlap)])


_HEADING = re.compile(r"(#{1,3})[ \t]+(\S.*?)[ \t]*")
_FENCE = re.compile(r"[ ]{0,3}(`{3,}|~{3,})")


def _markdown_sections(text: str) -> list[tuple[int, int, list[str]]]:
    """(start, end, headings in effect) per section, from ATX headings (# to ###) at line starts.
    Headings inside fenced code blocks are ignored. Text before the first heading is a section too."""
    boundaries: list[tuple[int, list[str]]] = [(0, [])]
    stack: list[str] = []
    fence: str | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        marker = _FENCE.match(content)
        if fence is not None:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
        elif marker:
            fence = marker.group(1)
        elif m := _HEADING.fullmatch(content):
            del stack[len(m.group(1)) - 1 :]
            stack.extend([""] * (len(m.group(1)) - 1 - len(stack)))
            stack.append(m.group(2))
            boundaries.append((offset, [h for h in stack if h]))
        offset += len(line)
    ends = [start for start, _ in boundaries[1:]] + [len(text)]
    return [(start, end, headings) for (start, headings), end in zip(boundaries, ends)]


class MarkdownChunker:
    def chunk(self, doc: ParsedDocument, params: ChunkingParams) -> list[ChunkDraft]:
        splitter = _recursive(params)
        spans: list[tuple[int, int, dict]] = []
        for start, end, headings in _markdown_sections(doc.text):
            body = doc.text[start:end]
            for s, e in _locate(body, splitter.split_text(body), params.chunk_overlap, base=start):
                spans.append((s, e, {"headings": headings}))
        return _finish(doc, spans)


def _recursive(params: ChunkingParams) -> "RecursiveCharacterTextSplitter":
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    return RecursiveCharacterTextSplitter(
        chunk_size=params.chunk_size, chunk_overlap=params.chunk_overlap, add_start_index=True
    )
