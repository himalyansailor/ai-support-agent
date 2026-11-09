import io
import re
import zipfile

from supportcore.knowledge.parsing.ports import ParsedDocument, ParseError, require_text

MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 2000
_HEADING = re.compile(r"^Heading (\d)$")


class DocxParser:
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members = z.infolist()
        except zipfile.BadZipFile:
            raise ParseError("This document could not be read") from None
        if len(members) > MAX_MEMBERS or sum(m.file_size for m in members) > MAX_UNCOMPRESSED_BYTES:
            raise ParseError("Document is too large to process")

        try:
            document = Document(io.BytesIO(data))
            lines: list[str] = []
            for child in document.element.body.iterchildren():
                if child.tag.endswith("}p"):
                    line = _paragraph_line(Paragraph(child, document))
                    if line:
                        lines.append(line)
                elif child.tag.endswith("}tbl"):
                    for row in Table(child, document).rows:
                        seen = set()
                        for cell in row.cells:
                            if id(cell._tc) in seen:  # merged cells repeat the same element
                                continue
                            seen.add(id(cell._tc))
                            if cell.text.strip():
                                lines.append(cell.text.strip())
        except Exception:  # python-docx raises assorted errors on malformed packages
            raise ParseError("This document could not be read") from None
        return ParsedDocument(text=require_text("\n".join(lines)))


def _paragraph_line(paragraph) -> str:
    text = paragraph.text.strip()
    if not text:
        return ""
    name = paragraph.style.name if paragraph.style is not None else ""
    if name == "Title":
        return f"# {text}"
    match = _HEADING.match(name)
    return f"{'#' * int(match.group(1))} {text}" if match else text
