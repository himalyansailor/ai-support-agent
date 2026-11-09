import pytest

from supportcore.widget.domains import InvalidDomainError, frame_ancestors, normalize_domain, origin_allowed

WEB = "http://localhost:3000"


def test_normalize_domain():
    assert normalize_domain("HTTPS://Shop.Example.com/") == "shop.example.com"
    assert normalize_domain("*.Example.com") == "*.example.com"
    assert normalize_domain("localhost:5500") == "localhost:5500"
    assert normalize_domain("  http://Shop.example.com:8080 ") == "shop.example.com:8080"


@pytest.mark.parametrize("entry", ["*", "exa mple.com", "http://", "a.com/path?x", "a.com/path", "*.*.com", "", "a.com:99999", "*.com"])
def test_normalize_rejects(entry):
    with pytest.raises(InvalidDomainError):
        normalize_domain(entry)


def test_origin_allowed_wildcard_matches_subdomain_not_apex():
    domains = ["*.example.com"]
    assert origin_allowed("https://shop.example.com", domains, web_origin=WEB)
    assert origin_allowed("https://a.b.example.com", domains, web_origin=WEB)
    assert not origin_allowed("https://example.com", domains, web_origin=WEB)
    assert not origin_allowed("https://evilexample.com", domains, web_origin=WEB)
    assert not origin_allowed("https://shop.example.com.evil.io", domains, web_origin=WEB)


def test_origin_allowed_requires_https_except_localhost():
    assert not origin_allowed("http://shop.example.com", ["shop.example.com"], web_origin=WEB)
    assert origin_allowed("https://shop.example.com", ["shop.example.com"], web_origin=WEB)
    assert origin_allowed("http://localhost:5500", ["localhost:5500"], web_origin=WEB)
    assert not origin_allowed("http://localhost:5501", ["localhost:5500"], web_origin=WEB)
    assert origin_allowed("http://127.0.0.1:8000", ["127.0.0.1:8000"], web_origin=WEB)


def test_origin_allowed_web_origin_always_and_garbage_rejected():
    assert origin_allowed(WEB, [], web_origin=WEB)
    assert not origin_allowed("https://shop.example.com", [], web_origin=WEB)
    assert not origin_allowed("null", ["shop.example.com"], web_origin=WEB)
    assert not origin_allowed("", ["shop.example.com"], web_origin=WEB)


def test_frame_ancestors_contains_web_origin_and_entries():
    value = frame_ancestors(["shop.example.com", "*.foo.org", "localhost:5500"], WEB)
    assert value.startswith("frame-ancestors ")
    parts = value.split()
    assert WEB in parts
    assert "https://shop.example.com" in parts and "https://*.foo.org" in parts
    assert "https://localhost:5500" in parts and "http://localhost:5500" in parts
    assert "http://shop.example.com" not in parts
    assert frame_ancestors([], WEB) == f"frame-ancestors {WEB}"
