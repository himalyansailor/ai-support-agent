import pytest

from supportcore.mail.sanitize import html_to_text, sanitize_html

HOSTILE = """
<p onclick="x()" onmouseover=y()>Hi
<a href="javascript:alert(1)">a</a>
<a href="JaVaScRiPt:alert(1)">b</a>
<a href="  java&#x73;cript:alert(1)">c</a>
<a href="java&#x09;script:alert(1)">d</a>
<a href="&#x01;javascript:alert(1)">e</a>
<a href="vbscript:x">f</a>
<a href="/relative">g</a>
<img src="x" onerror="alert(1)">
<img src="javascript:alert(1)">
<div style="width:expression(alert(1));color:red">s1</div>
<div style="background:url(javascript:alert(1))">s2</div>
<div style="background:url( 'ftp://x.test/a' )">s3</div>
<div style="background:u\\72l(http://t.test/a)">s4</div>
</p>
<script>alert(1)</script>
<form action="https://evil.test"><input name=a><button>go</button></form>
<meta http-equiv="refresh" content="0;url=https://evil.test">
<iframe src="https://evil.test"></iframe>
<svg onload="alert(1)"><script>alert(1)</script></svg>
<object data="x"></object><embed src="x"><base href="https://evil.test/"><link rel="stylesheet" href="https://evil.test/a.css">
<style>body{background:url(https://t.test/x)}</style>
<!-- comment --><p>end</p>
"""


def test_sanitize_strips_active_content():
    out = sanitize_html(HOSTILE).html
    assert out is not None
    low = out.lower()
    for bad in (
        "<script", "onerror", "onclick", "onmouseover", "onload", "javascript:", "vbscript", "<form", "<input",
        "<iframe", "<meta", "<svg", "<object", "<embed", "<base", "<link", "<style", "expression", "alert(",
        "evil.test", "ftp:", 'href="/relative"', "<!--", "t.test",
    ):  # fmt: skip
        assert bad not in low, bad
    assert "hi" in low and "end" in low and "color:red" in low


def test_remote_images_detected():
    assert sanitize_html('<img src="https://t.test/p.png">').has_remote_images
    assert sanitize_html('<div style="background:url(https://t.test/p.png)">x</div>').has_remote_images
    assert sanitize_html('<div style="background:url(\'http://t.test/p.png\')">x</div>').has_remote_images
    assert not sanitize_html("<p>text</p>").has_remote_images
    assert not sanitize_html('<img src="data:image/png;base64,AAAA">').has_remote_images
    assert not sanitize_html('<img src="cid:logo">').has_remote_images


def test_data_uri_only_on_img():
    out = sanitize_html(
        '<img src="data:image/png;base64,AAAA"><a href="data:text/html;base64,AAAA">x</a>'
        '<img src="data:text/html;base64,AAAA"><div style="background:url(data:image/png;base64,AAAA)">y</div>'
    ).html
    assert out.count("data:image/png") == 2  # the img and the style
    assert "data:text/html" not in out


def test_links_get_noopener():
    out = sanitize_html('<a href="https://e.test/x" target="_self" rel="opener">l</a><a href="mailto:a@example.test">m</a>').html
    assert out.count('rel="noopener noreferrer nofollow"') == 2
    assert out.count('target="_blank"') == 2
    assert "_self" not in out


def test_output_over_1mb_is_dropped():
    r = sanitize_html("<p>" + "a" * 1_100_000 + "</p>")
    assert r.html is None and not r.has_remote_images


@pytest.mark.parametrize(
    "html", ["<div>" * 50_000, "<<<>>>&#;<p", "", "\x00<b>"], ids=["deep-divs", "broken-markup", "empty", "nul"]
)
def test_hostile_input_does_not_raise(html):
    sanitize_html(html)
    html_to_text(html)


def test_html_to_text_blocks():
    assert html_to_text("<p>A</p><p>B<br>C</p><script>x</script>") == "A\n\nB\nC"


def test_image_set_urls_are_checked_and_flagged():
    r = sanitize_html("<div style=\"background-image:image-set('https://t.test/a.png' 1x)\">x</div>")
    assert r.has_remote_images and "t.test" in r.html
    r = sanitize_html('<div style="background-image:-webkit-image-set(url(https://t.test/a.png) 1x)">x</div>')
    assert r.has_remote_images
    r = sanitize_html("<div style=\"background-image:image-set('ftp://t.test/a.png' 1x)\">x</div>")
    assert "ftp" not in r.html and not r.has_remote_images
    r = sanitize_html("<div style=\"background-image:image-set('javascript:1' 1x)\">x</div>")
    assert "javascript" not in r.html
    r = sanitize_html("<div style=\"background-image:image-set('data:image/png;base64,AAAA' 1x)\">x</div>")
    assert "data:image/png" in r.html and not r.has_remote_images


def test_position_dropped_and_class_restricted():
    out = sanitize_html(
        '<div style="position:fixed;top:0;color:red">x</div><div class="gmail_quote evil">q</div><blockquote class="evil">b</blockquote>'
    ).html
    assert "position" not in out and "top:0" in out and "color:red" in out
    assert 'class="gmail_quote"' in out and "evil" not in out


# Time-budget regressions: each shape took more than 10 s before the bounds existed.
def _timed(fn):
    import time

    t = time.perf_counter()
    result = fn()
    assert time.perf_counter() - t < 5.0
    return result


def test_dos_unclosed_css_urls():
    for payload in ("url(" * 16_000, "url('a " * 8_000):
        r = _timed(lambda: sanitize_html(f'<span style="{payload}">x</span>'))
        assert r.html is not None and "url" not in r.html
    # inside the 8 KB cap but still hostile
    _timed(lambda: sanitize_html('<span style="' + "url(" * 2_000 + '">x</span>'))


@pytest.mark.parametrize(
    "html",
    ["<p>x</p>" * 300_000, "<div/>" * 20_000, "<div></span>" * 30_000, "<div>" * 50_000, "<a" * 1_000_000, "<!--" * 400_000],
    ids=["many-paragraphs", "self-closed-divs", "mismatched-closers", "unclosed-divs", "open-tag-starts", "comment-starts"],
)
def test_dos_markup_shapes(html):
    r = _timed(lambda: sanitize_html(html))
    assert r.html is None or len(r.html) <= 1_000_000
    _timed(lambda: html_to_text(html))


def test_deep_nesting_is_detected_even_with_slash_and_mismatched_closers():
    assert sanitize_html("<div/>" * 300).html is None
    assert sanitize_html("<div></span>" * 300).html is None
    assert sanitize_html("<table>" + "<tr><td>a<td>b" * 500 + "</table>").html is not None  # implied end tags


@pytest.mark.parametrize("n", [4000, 40000])
@pytest.mark.parametrize(
    "unit", ["<div><b title={i}></div>", "<p><b title={i}></p>", "<div><a href=x{i}></div>"], ids=["div-b", "p-b", "div-a"]
)
def test_dos_formatting_element_reconstruction(n, unit):
    html = "".join(unit.format(i=i) for i in range(n)) + "x"
    r = _timed(lambda: sanitize_html(html))
    assert r.html is None
    assert "x" in _timed(lambda: html_to_text(html))


def test_dos_formatting_times_tags_product_and_misnesting_shapes():
    for html in (
        "".join(f"<div><b title={i}></div>" for i in range(99)) + "<div>x</div>" * 20_000,
        "<table><b title=1><tr><td>x</table>" * 5000,
        "<b title=1><p>x</b>" * 45_000,
        "<table><tr><td>" * 150 + "x" + "</table>" * 150,
    ):
        _timed(lambda: sanitize_html(html))
        _timed(lambda: html_to_text(html))


def test_text_fallback_for_complex_markup_keeps_text_and_breaks():
    html = "".join(f"<div>line{i}<b title={i}></div>" for i in range(200))
    text = html_to_text(html)
    assert text.splitlines()[0] == "line0" and "line199" in text and "<" not in text
    assert sanitize_html("<p>ok <b>bold</b> <a href='https://e.test'>l</a></p>").html is not None


def test_dos_formatting_closers_hidden_in_comments_and_attributes():
    for html in (
        "".join(f"<div><b title={i}></div><!--</b>-->" for i in range(8000)) + "x",
        "".join(f'<div><b title={i}></div><span title="</b>"></span>' for i in range(8000)) + "x",
        "".join(f"<div><b title={i}></div><style></b></style>" for i in range(8000)) + "x",
    ):
        assert _timed(lambda: sanitize_html(html)).html is None
        assert "x" in _timed(lambda: html_to_text(html))


def test_dos_many_attributes_on_one_element():
    shapes = [
        "<p " + " ".join(f"a{i}" for i in range(100_000)) + ">x</p>",
        '<p title=">" ' + " ".join(f"a{i}" for i in range(100_000)) + ">x</p>",
        "<p " + "".join(f'a{i}="1"' for i in range(100_000)) + ">x</p>",
        "<p " + "/".join(f"a{i}" for i in range(100_000)) + ">x</p>",
        "<p " + " ".join(f"a{i}" for i in range(300_000)) + ">x</p>" * 3,  # over the input cap
    ]
    for html in shapes:
        assert _timed(lambda: sanitize_html(html)).html is None
        text = _timed(lambda: html_to_text(html))
        assert "x" in text and "a99999" not in text


def test_ordinary_html_with_many_ordinary_attributes_is_kept():
    html = '<table width="600" cellpadding="0" cellspacing="0" style="' + "color: red; " * 100 + '"><tr><td>x</td></tr></table>'
    assert sanitize_html(html).html is not None
    assert sanitize_html("<a href='https://e.test/a'>l</a>" * 300).html is not None
