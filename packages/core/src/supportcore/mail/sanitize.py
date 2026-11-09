"""Sanitises inbound HTML mail with nh3 and turns HTML into text.

The output is shown in a sandboxed frame, but it is still made safe on its own: only an email-safe tag and
attribute set survives, links are http(s) or mailto, images are http(s) or data:image on `img` only, and `style`
attributes lose anything that can run code or fetch a non-http(s) resource. Remote images are reported so the
frame can block them until the agent asks to load them.

Everything here is linear or bounded: a style attribute over 8 KB is dropped, CSS urls are scanned by hand,
markup nested deeper than 200 levels (as an HTML parser would nest it), with more than 1 000 formatting start tags (or
too many with many tags), with more than 200 attributes on an element, with more than 50 000 tags, or longer than 2 M characters keeps no HTML, and the text extraction is nh3 plus two regexes (no tree).
"""

import re
from dataclasses import dataclass
from html import unescape

import nh3

MAX_HTML_BYTES = 1_000_000
MAX_INPUT_CHARS = 2_000_000
MAX_STYLE_CHARS = 8192
MAX_HTML_NESTING = 200
MAX_TAGS = 50_000
MAX_TEXT_INPUT_CHARS = MAX_INPUT_CHARS
MAX_FORMATTING_STARTS = 1000
MAX_ATTRIBUTES_PER_ELEMENT = 200
MAX_ATTRIBUTES_TOTAL = 100_000
MAX_RECONSTRUCTION_WORK = 1_000_000
QUOTE_CLASSES = frozenset({"gmail_quote", "gmail_attr", "yahoo_quoted", "moz-cite-prefix", "OutlookMessageHeader"})

_TAGS = {
    "a", "abbr", "address", "b", "big", "blockquote", "br", "caption", "center", "cite", "code", "col", "colgroup",
    "dd", "del", "div", "dl", "dt", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "li",
    "ol", "p", "pre", "q", "s", "small", "span", "strike", "strong", "sub", "sup", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "u", "ul",
}  # fmt: skip
_CLEAN_CONTENT_TAGS = {
    "script", "style", "iframe", "object", "embed", "svg", "math", "noscript", "template", "title", "head",
    "textarea", "select", "option", "applet", "frameset", "frame", "noframes", "xmp", "plaintext",
}  # fmt: skip
_COMMON_ATTRS = {"title", "width", "height", "style", "align"}
_ATTRIBUTES: dict[str, set[str]] = {
    "*": _COMMON_ATTRS,
    "a": {"href"},
    "img": {"src", "alt"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}  # `class` is allowed only through allowed_classes (quote markers)
_ALLOWED_CLASSES = {"div": set(QUOTE_CLASSES), "blockquote": set(QUOTE_CLASSES)}
_URL_SCHEMES = {"http", "https", "mailto", "data"}
_DATA_IMAGE = re.compile(r"^data:image/(png|jpe?g|gif|webp|bmp);", re.IGNORECASE)
_CTRL_AND_SPACE = re.compile(r"[\x00-\x20\x7f-\x9f​-‏  ﻿]")
_CSS_COMMENT = re.compile(r"/\*.*?(\*/|$)", re.DOTALL)  # lazy but anchored to a following "*/" or the end: linear
_CSS_BAD = re.compile(r"expression\s*\(|javascript:|vbscript:|behaviou?r\s*:|-moz-binding|@import|\\|<|>", re.IGNORECASE)
_DROPPED_PROPERTIES = frozenset({"position"})
_OTHER_URL_FUNCTIONS = ("image(", "cross-fade(", "element(", "src(", "paint(")


@dataclass(frozen=True)
class SanitizedHtml:
    html: str | None
    has_remote_images: bool


def _split_declarations(value: str) -> list[str]:
    """Splits on `;` outside quotes and parentheses (a data: url holds a `;`)."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote = ""
    for ch in value:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return parts


def _css_urls(declaration: str) -> list[str] | None:
    """Every `url(...)` value, found by one forward scan; None when one is malformed (unclosed)."""
    low = declaration.lower()
    n = len(declaration)
    urls: list[str] = []
    i = 0
    while True:
        j = low.find("url(", i)
        if j < 0:
            return urls
        k = j + 4
        while k < n and declaration[k] in " \t\r\n\f":
            k += 1
        if k < n and declaration[k] in "\"'":
            end = declaration.find(declaration[k], k + 1)
            if end < 0:
                return None
            value = declaration[k + 1 : end]
            k = end + 1
            while k < n and declaration[k] in " \t\r\n\f":
                k += 1
            if k >= n or declaration[k] != ")":
                return None
            i = k + 1
        else:
            end = declaration.find(")", k)
            if end < 0:
                return None
            value = declaration[k:end].strip()
            if any(c in value for c in "\"'("):
                return None
            i = end + 1
        urls.append(value)


def _quoted_strings(declaration: str) -> list[str] | None:
    strings: list[str] = []
    i = 0
    n = len(declaration)
    while i < n:
        if declaration[i] in "\"'":
            end = declaration.find(declaration[i], i + 1)
            if end < 0:
                return None
            strings.append(declaration[i + 1 : end])
            i = end + 1
        else:
            i += 1
    return strings


def _is_remote(url: str) -> bool:
    return _CTRL_AND_SPACE.sub("", url).lower().startswith(("http://", "https://"))


def _is_allowed_style_url(url: str) -> bool:
    return _is_remote(url) or bool(_DATA_IMAGE.match(_CTRL_AND_SPACE.sub("", url)))


def _clean_style(value: str, flags: dict[str, bool]) -> str | None:
    """Keeps only declarations that cannot run code; url values must be http(s) or data:image."""
    if len(value) > MAX_STYLE_CHARS:
        return None
    value = _CSS_COMMENT.sub("", value)
    kept: list[str] = []
    for declaration in _split_declarations(value):
        declaration = declaration.strip()
        if not declaration or _CSS_BAD.search(declaration):
            continue
        prop = declaration.split(":", 1)[0].strip().lower()
        if prop in _DROPPED_PROPERTIES:
            continue
        low = declaration.lower()
        if any(fn in low for fn in _OTHER_URL_FUNCTIONS):
            continue
        urls = _css_urls(declaration)
        if urls is None:
            continue
        if "image-set(" in low:
            strings = _quoted_strings(declaration)
            if strings is None:
                continue
            # A quoted string in image-set is a url, except type("image/png").
            urls = urls + [s for s in strings if not s.lower().startswith("image/") and s not in urls]
        if not all(_is_allowed_style_url(u) for u in urls):
            continue
        if any(_is_remote(u) for u in urls):
            flags["remote"] = True
        kept.append(declaration)
    return "; ".join(kept) or None


def _make_filter(flags: dict[str, bool]):
    def attribute_filter(tag: str, attr: str, value: str) -> str | None:
        if attr == "style":
            return _clean_style(value, flags)
        if attr in ("href", "src"):
            compact = _CTRL_AND_SPACE.sub("", value).lower()
            if attr == "href":
                return value.strip() if compact.startswith(("http://", "https://", "mailto:")) else None
            if tag == "img" and (compact.startswith(("http://", "https://")) or _DATA_IMAGE.match(compact)):
                if compact.startswith(("http://", "https://")):
                    flags["remote"] = True
                return value.strip()
            return None
        return value

    return attribute_filter


_TAG_START = re.compile(r"<(/?)([a-zA-Z][^\s/>]*)")
_VOID = frozenset({"br", "hr", "img", "meta", "link", "input", "col", "base", "area", "wbr", "source", "param"})
_FORMATTING = frozenset({"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong", "tt", "u"})
_IMPLIED_END = frozenset({"p", "li", "td", "th", "tr", "dd", "dt", "option", "thead", "tbody", "tfoot", "caption"})


_TAG_REST = re.compile(r"""(?:[^>"']+|"[^"]*"|'[^']*')*""")  # the rest of a start tag; always matches, no backtracking
_ATTR_TOKEN = re.compile(r"""\"[^"]*\"|'[^']*'|[^\s"'=/]+""")


def _attribute_tokens(segment: str, cap: int) -> int:
    """Names and values in a start tag's text, counting a quoted value as one token; stops counting above `cap`."""
    count = 0
    for _ in _ATTR_TOKEN.finditer(segment):
        count += 1
        if count > cap:
            break
    return count


def _too_complex(html: str, limit: int = MAX_HTML_NESTING) -> bool:
    """One linear pre-scan: is this markup too nested or too busy for nh3 to handle in bounded time?

    This is the fast path; `isolate.py` is the hard bound for whatever it misses. Budgets: an upper estimate of
    the nesting depth an HTML parser would build (the `/` of `<div/>` is ignored, a closing tag that matches
    nothing open is ignored, a closing tag closes everything above its match, and `p`/`li`/`td`... close each
    other the way HTML implies); the number of formatting-element start tags (the parser re-opens unclosed ones
    after every block close, which is super-quadratic; closing tags never lower the count, because a closer
    inside a comment or attribute is not a closer); the product of that count and the tag count; the total tag
    count; and the attributes of every start tag. Tags and closers inside comments, attribute values or raw text
    are all counted, which can only over-count.
    """
    stack: list[str] = []
    open_count: dict[str, int] = {}
    formatting_starts = 0
    tags = 0
    attribute_total = 0
    segment_end = 0
    for match in _TAG_START.finditer(html):
        tags += 1
        if tags > MAX_TAGS:
            return True
        name = match.group(2).lower()
        if match.group(1):
            if open_count.get(name):
                while stack:
                    top = stack.pop()
                    open_count[top] -= 1
                    if top == name:
                        break
            continue
        if match.end() >= segment_end:  # not inside the text of the previous start tag
            rest = _TAG_REST.match(html, match.end())
            segment_end = rest.end() if rest.end() >= len(html) or html[rest.end()] == ">" else len(html)
            tokens = _attribute_tokens(html[match.end() : segment_end], 2 * MAX_ATTRIBUTES_PER_ELEMENT)
            attribute_total += tokens
            if tokens > 2 * MAX_ATTRIBUTES_PER_ELEMENT or attribute_total > 2 * MAX_ATTRIBUTES_TOTAL:
                return True
        if name in _FORMATTING:
            formatting_starts += 1
            if formatting_starts > MAX_FORMATTING_STARTS:
                return True
        if name in _VOID:
            continue
        if name in _IMPLIED_END:
            while stack and stack[-1] in _IMPLIED_END:
                open_count[stack.pop()] -= 1
        stack.append(name)
        open_count[name] = open_count.get(name, 0) + 1
        if len(stack) > limit:
            return True
    return formatting_starts * tags > MAX_RECONSTRUCTION_WORK


def sanitize_html(html: str) -> SanitizedHtml:
    if len(html) > MAX_INPUT_CHARS or _too_complex(html):
        return SanitizedHtml(None, False)
    flags = {"remote": False}
    try:
        cleaned = nh3.clean(
            html,
            tags=_TAGS,
            clean_content_tags=_CLEAN_CONTENT_TAGS,
            attributes=_ATTRIBUTES,
            attribute_filter=_make_filter(flags),
            strip_comments=True,
            link_rel="noopener noreferrer nofollow",
            set_tag_attribute_values={"a": {"target": "_blank"}},
            url_schemes=_URL_SCHEMES,
            allowed_classes=_ALLOWED_CLASSES,
        )
    except BaseException as exc:  # noqa: BLE001 - nh3 panics arrive as BaseException; a hostile document never raises
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return SanitizedHtml(None, False)
    if len(cleaned.encode("utf-8", errors="replace")) > MAX_HTML_BYTES:
        return SanitizedHtml(None, False)
    return SanitizedHtml(cleaned, flags["remote"])


_BLOCK_TAGS = frozenset({
    "p", "div", "tr", "li", "ul", "ol", "table", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    "dl", "dt", "dd", "address", "center", "br",
})  # fmt: skip
_TEXT_TAGS = _BLOCK_TAGS | {"td", "th"}
_BREAK_TAG = re.compile(r"</?(?:" + "|".join(sorted(_BLOCK_TAGS)) + r")\s*/?>", re.IGNORECASE)
_CELL_TAG = re.compile(r"</?(?:td|th)\s*/?>", re.IGNORECASE)
_LT = re.compile(r"<(?=[a-zA-Z/!?])")


def _strip_tags(html: str) -> str:
    """Linear tag removal (block tags become line breaks); each tag is consumed once, quotes in tags are honoured."""
    out: list[str] = []
    pos = 0
    while True:
        match = _LT.search(html, pos)
        if match is None:
            out.append(html[pos:])
            break
        out.append(html[pos : match.start()])
        name = re.match(r"/?([a-zA-Z0-9]*)", html[match.end() : match.end() + 12])
        rest = _TAG_REST.match(html, match.end())
        end = rest.end()
        out.append("\n" if name and name.group(1).lower() in _BLOCK_TAGS else " ")
        pos = end + 1 if end < len(html) and html[end] == ">" else len(html)
        if pos >= len(html):
            break
    return "".join(out)


def html_to_text(html: str) -> str:
    """Plain text with a line break at block elements.

    nh3 (linear, unlike the standard-library HTML parser and BeautifulSoup, which are quadratic on some inputs)
    first reduces the markup to well-formed block tags and escaped text; then two regexes and `html.unescape`
    finish the job. Markup that is too nested or too busy (`_too_complex`) skips nh3 and just loses its tags by regex.
    """
    if len(html) > MAX_TEXT_INPUT_CHARS or _too_complex(html):
        text = unescape(_strip_tags(html))
    else:
        try:
            cleaned = nh3.clean(
                html, tags=set(_TEXT_TAGS), attributes={}, clean_content_tags=set(_CLEAN_CONTENT_TAGS), strip_comments=True
            )
        except BaseException as exc:  # noqa: BLE001 - see sanitize_html
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            cleaned = _strip_tags(html)
        text = unescape(_CELL_TAG.sub(" ", _BREAK_TAG.sub("\n", cleaned)))
    text = text.replace("\xa0", " ")
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()
