"""Calls a business tool's HTTP endpoint through the hardened client (no redirects, vetted addresses, capped body)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx

from supportcore.ai.errors import CredentialInvalidError, ProviderResponseError
from supportcore.ai.http import RESPONSE_COMPRESSED, guarded_async_client
from supportcore.config import Settings
from supportcore.tools.ports import ToolResult, ToolSnapshot
from supportcore.tools.schema import PLACEHOLDER

TOOL_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 102_400
MAX_TEXT_CHARS = 8000


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class HttpToolExecutor:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._allow_private = settings.effective_allow_private_base_urls()
        self._transport = transport  # tests only: replaces the network transport

    async def call(self, tool: ToolSnapshot, args: dict[str, Any], auth_value: str | None) -> ToolResult:
        started = time.monotonic()

        def finish(error: str | None, status: int | None = None, text: str = "") -> ToolResult:
            return ToolResult(
                ok=error is None,
                status=status,
                text=text[:MAX_TEXT_CHARS],
                latency_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )

        names = set(PLACEHOLDER.findall(tool.url_template))
        if any(n not in args or _text(args[n]).strip() in ("", ".", "..") for n in names):
            return finish("invalid_arguments")
        url = PLACEHOLDER.sub(lambda m: quote(_text(args[m.group(1)]), safe=""), tool.url_template)
        rest = {k: v for k, v in args.items() if k not in names}
        headers = {}
        if tool.auth_header_name and auth_value:
            headers[tool.auth_header_name] = auth_value
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
                async with guarded_async_client(
                    allow_private=self._allow_private,
                    timeout=TOOL_TIMEOUT_SECONDS,
                    max_response_bytes=MAX_RESPONSE_BYTES,
                    transport=self._transport,
                ) as client:
                    if tool.method == "POST":
                        response = await client.post(url, json=rest, headers=headers)
                    else:
                        params = {k: _text(v) for k, v in rest.items()}
                        response = await client.get(url, params=params, headers=headers)
                    body = response.text
        except (TimeoutError, httpx.TimeoutException):
            return finish("timeout")
        except CredentialInvalidError:
            return finish("blocked_address")
        except ProviderResponseError as exc:
            return finish("network_error" if str(exc) == RESPONSE_COMPRESSED else "response_too_large")
        except Exception:  # never raises: httpx errors, bad URLs, anything else
            return finish("network_error")
        if not response.is_success:
            return finish(f"http_{response.status_code}", response.status_code, body)
        return finish(None, response.status_code, body)
