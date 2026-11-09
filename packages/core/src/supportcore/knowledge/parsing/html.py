import re

from supportcore.knowledge.parsing.ports import ParsedDocument, ParseError, decode_text, require_text

_DROP_TAGS = ("script", "style", "nav", "footer", "header")


class HtmlParser:
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        from bs4 import BeautifulSoup

        source = decode_text(data)
        try:
            soup = BeautifulSoup(source, "html.parser")
            title_tag = soup.find("title")
            title = re.sub(r"\s+", " ", title_tag.get_text()).strip() if title_tag else None
            for tag in soup(_DROP_TAGS + ("title",)):
                tag.decompose()
            text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        except Exception:
            raise ParseError("This HTML file could not be read") from None
        return ParsedDocument(text=require_text(text), title=title or None)
