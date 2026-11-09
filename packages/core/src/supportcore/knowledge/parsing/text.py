from supportcore.knowledge.parsing.ports import ParsedDocument, decode_text, require_text


class TextParser:
    """Plain text and Markdown (Markdown is kept as-is for the markdown chunker)."""

    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        return ParsedDocument(text=require_text(decode_text(data)))
