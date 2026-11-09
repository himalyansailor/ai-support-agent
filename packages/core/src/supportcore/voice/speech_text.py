"""Turn the bot's markdown reply into text a TTS voice can say, a sentence at a time."""

import re

MAX_SPOKEN_CHARS = 1200
_MAX_RUN = 200
_CLOSERS = "\"')]\u201d\u2019"

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_FENCE_LINE = re.compile(r"^\s*(?:```|~~~).*$")
_LINK = re.compile(r"!?\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+")
_URL_TRAIL = ".,;:!?)\"'"
_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_MARKER_ONLY = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*$")
_RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s*(?:>\s*)+")
_TABLE_SEP = re.compile(r"^[\s|:-]+$")
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w])_(?=\S)([^_\n]+?)_(?![\w])")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d]")
_PUA = 0xE000


def _line_text(raw: str) -> str:
    """One markdown line -> speakable text ("" when the line carries nothing to say)."""
    if _FENCE_LINE.match(raw) or _MARKER_ONLY.match(raw) or _RULE.match(raw):
        return ""
    line = _QUOTE.sub("", raw)
    ends_sentence = bool(_LIST.match(line) or _HEADING.match(line))
    line = _HEADING.sub("", _LIST.sub("", line))
    if "|" in line:
        if _TABLE_SEP.match(line):
            return ""
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        line = ", ".join(c for c in cells if c)
        ends_sentence = True
    line = _BOLD.sub(r"\2", line)
    line = _ITALIC_STAR.sub(r"\1", line)
    line = _ITALIC_UNDER.sub(r"\1", line)
    line = line.replace("`", "").replace("~~", "").strip()
    if line and ends_sentence and line[-1] not in ".!?:;,":
        line += "."
    return line


def _strip_url(match: re.Match[str]) -> str:
    url = match.group(0)
    return url[len(url.rstrip(_URL_TRAIL)) :]


def to_speech_text(markdown: str) -> str:
    text = _FENCE.sub(" ", markdown)
    # protect backslash-escaped characters from every pass below, restore at the end
    text = _ESCAPE.sub(lambda m: chr(_PUA + ord(m.group(1))), text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(_strip_url, text)
    text = _HTML_TAG.sub("", text)
    text = _EMOJI.sub("", text)
    lines = [t for t in (_line_text(raw) for raw in text.splitlines()) if t]
    out = re.sub(r"\s+", " ", " ".join(lines))
    out = re.sub(r"\s+([.,;:!?)])", r"\1", out)
    out = "".join(chr(ord(c) - _PUA) if _PUA <= ord(c) < _PUA + 0x80 else c for c in out)
    return out.strip()


class SentenceSplitter:
    """Streaming sentence splitter for LLM deltas."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        while True:
            cut = self._find_end()
            if cut is None:
                break
            end, skip = cut
            sentence = self._buf[:end].strip()
            self._buf = self._buf[end + skip :].lstrip()
            if sentence:
                out.append(sentence)
        while len(self._buf) > _MAX_RUN:
            window = self._buf[: _MAX_RUN + 1]
            ws = max(window.rfind(" "), window.rfind("\t"))
            end = ws if ws > 0 else _MAX_RUN
            out.append(self._buf[:end].strip())
            self._buf = self._buf[end:].lstrip()
        return out

    def _find_end(self) -> tuple[int, int] | None:
        buf = self._buf
        for i, ch in enumerate(buf):
            if ch == "\n":
                return i, 1
            if ch in ".!?":
                j = i + 1
                while j < len(buf) and buf[j] in _CLOSERS:
                    j += 1
                if j < len(buf) and buf[j].isspace():
                    return j, 0
        return None

    def flush(self) -> str | None:
        rest = self._buf.strip()
        self._buf = ""
        return rest or None


def cap_spoken(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    if len(text) <= limit:
        return text
    window = text[:limit]
    boundary_ok = text[limit].isspace()
    best = -1
    for i, ch in enumerate(window):
        if ch in ".!?" and ((i + 1 < len(window) and window[i + 1].isspace()) or (i + 1 == len(window) and boundary_ok)):
            best = i
    if best >= 0:
        return window[: best + 1].strip()
    if boundary_ok:
        return window.strip()
    ws = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
    return (window[:ws] if ws > 0 else window).strip()
