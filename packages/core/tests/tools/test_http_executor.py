import json
import uuid

import httpx
import pytest

from supportcore.config import Settings
from supportcore.tools.http_executor import MAX_TEXT_CHARS, HttpToolExecutor
from supportcore.tools.ports import ToolSnapshot


def _settings(**kw) -> Settings:
    return Settings(
        _env_file=None, env="test", jwt_secret="test-secret-test-secret-test-secret-123", **kw
    )


def _tool(method="GET", url="https://api.shop.com/orders/{order_id}", auth=("X-Api-Key", "k")) -> ToolSnapshot:
    return ToolSnapshot(
        id=uuid.uuid4(), name="t", description="d", method=method, url_template=url,
        input_schema={"type": "object", "properties": {}}, read_only=True,
        auth_header_name=auth[0] if auth else None,
    )


def _executor(handler, **kw) -> HttpToolExecutor:
    return HttpToolExecutor(_settings(**kw), transport=httpx.MockTransport(handler))


async def test_get_substitutes_and_encodes_placeholder():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text='{"status": "shipped"}')

    result = await _executor(handler).call(_tool(), {"order_id": "a/b", "verbose": True}, None)
    assert result.ok and result.status == 200 and result.error is None and "shipped" in result.text
    assert seen[0].url.raw_path == b"/orders/a%2Fb?verbose=true"
    assert seen[0].method == "GET"


async def test_post_sends_json():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={"ok": 1})

    tool = _tool(method="POST", url="https://api.shop.com/orders/{order_id}/notes")
    result = await _executor(handler).call(tool, {"order_id": "7", "note": "hi", "n": 3}, None)
    assert result.ok and result.status == 201
    assert seen[0].url.path == "/orders/7/notes" and seen[0].url.query == b""
    assert json.loads(seen[0].content) == {"note": "hi", "n": 3}


async def test_auth_header_sent_not_logged(capsys, caplog):
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(200, text="fine")

    caplog.set_level("DEBUG")
    await _executor(handler).call(_tool(), {"order_id": "1"}, "s3cret-value")
    assert seen[0]["X-Api-Key"] == "s3cret-value"
    captured = capsys.readouterr()
    assert "s3cret-value" not in captured.out + captured.err + caplog.text
    # No auth header when the tool has none, even if a value is passed.
    await _executor(handler).call(_tool(auth=None), {"order_id": "1"}, "s3cret-value")
    assert "X-Api-Key" not in seen[1]


async def test_timeout_returns_error_result():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    result = await _executor(handler).call(_tool(), {"order_id": "1"}, None)
    assert (result.ok, result.status, result.error) == (False, None, "timeout")


async def test_response_capped():
    def handler(request):
        return httpx.Response(200, content=b"x" * 200_000)

    result = await _executor(handler).call(_tool(), {"order_id": "1"}, None)
    assert (result.ok, result.error) == (False, "response_too_large")

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(30):  # no Content-Length: only the running total can refuse it
                yield b"x" * 10_000

    def streamed(request):
        return httpx.Response(200, stream=Chunks())

    result = await _executor(streamed).call(_tool(), {"order_id": "1"}, None)
    assert (result.ok, result.error) == (False, "response_too_large")


async def test_text_is_cut_for_the_model():
    result = await _executor(lambda r: httpx.Response(200, text="y" * 50_000)).call(_tool(), {"order_id": "1"}, None)
    assert result.ok and len(result.text) == MAX_TEXT_CHARS


async def test_private_url_refused():
    executor = HttpToolExecutor(_settings(allow_private_base_urls=False))
    result = await executor.call(_tool(url="http://10.0.0.5/x"), {}, None)
    assert (result.ok, result.error) == (False, "blocked_address")


@pytest.mark.parametrize("status", [404, 500, 302])
async def test_non_2xx_is_http_status_error(status):
    result = await _executor(lambda r: httpx.Response(status, text="nope")).call(_tool(), {"order_id": "1"}, None)
    assert (result.ok, result.status, result.error) == (False, status, f"http_{status}")
    assert result.text == "nope"


async def test_network_error_and_missing_placeholder_never_raise():
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    assert (await _executor(boom).call(_tool(), {"order_id": "1"}, None)).error == "network_error"
    assert (await _executor(boom).call(_tool(), {}, None)).error == "invalid_arguments"


@pytest.mark.parametrize("value", ["", ".", ".."])
async def test_dot_segment_placeholder_values_refused(value):
    called = []

    def handler(request):
        called.append(request)
        return httpx.Response(200, text="x")

    tool = _tool(url="https://api.shop.com/v1/orders/{order_id}/status")
    result = await _executor(handler).call(tool, {"order_id": value}, "tok")
    assert (result.ok, result.error) == (False, "invalid_arguments") and called == []


@pytest.mark.parametrize("value", ["../", "a?b#c@d", "%2e%2e", "x/../../y", "evil.com/", "a b"])
async def test_placeholder_values_are_encoded_and_cannot_change_host_or_path(value):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text="x")

    tool = _tool(url="https://api.shop.com/v1/orders/{order_id}/status")
    assert (await _executor(handler).call(tool, {"order_id": value}, None)).ok
    url = seen[0].url
    assert url.host == "api.shop.com" and url.query == b"" and url.fragment == ""
    raw = url.raw_path.decode()
    assert raw.startswith("/v1/orders/") and raw.endswith("/status")
    assert raw.count("/") == 4  # the value never adds or removes a path segment
    assert "%25" in raw if value == "%2e%2e" else True


async def test_arguments_are_not_logged_at_info(caplog, capsys):
    from supportcore.observability.logging import configure_logging

    configure_logging(_settings(log_level="INFO"))
    caplog.set_level("INFO")
    await _executor(lambda r: httpx.Response(200, text="ok")).call(
        _tool(), {"order_id": "placeholder-needle-1", "q": "query-needle-2"}, "auth-needle-3"
    )
    seen = caplog.text + capsys.readouterr().out
    assert "needle" not in seen
