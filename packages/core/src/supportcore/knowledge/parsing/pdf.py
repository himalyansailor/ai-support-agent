import io

from supportcore.knowledge.parsing.ports import PageSpan, ParsedDocument, ParseError, require_text

_PAGE_SEPARATOR = "\n\n"


class PdfParser:
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ParseError("This PDF is password-protected")
            page_texts = [(page.extract_text() or "").replace("\x00", "").strip() for page in reader.pages]
        except ParseError:
            raise
        except Exception:  # pypdf raises assorted errors on malformed files
            raise ParseError("This PDF could not be read") from None

        pages: list[PageSpan] = []
        offset = 0
        for number, text in enumerate(page_texts, start=1):
            pages.append(PageSpan(page=number, char_start=offset, char_end=offset + len(text)))
            offset += len(text) + len(_PAGE_SEPARATOR)
        return ParsedDocument(text=require_text(_PAGE_SEPARATOR.join(page_texts)), pages=pages)
