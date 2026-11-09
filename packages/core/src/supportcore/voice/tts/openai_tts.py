"""OpenAI streaming text-to-speech: PCM 24 kHz -> 8 kHz -> µ-law -> 160-byte frames. Input text is never logged."""

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from supportcore.config import Settings
from supportcore.voice.audio import Framer, Resampler24kTo8k, pcm16_to_mulaw
from supportcore.voice.tts.ports import TtsAuthError, TtsConfig, TtsError

REQUEST_TIMEOUT_SECONDS = 15
CHUNK_BYTES = 4800  # 100 ms of 24 kHz 16-bit PCM

ClientFactory = Callable[[str], Any]
MAX_CLIENTS = 128  # well above voice_max_calls, so an evicted (least recently used) client is idle


def _default_client(api_key: str) -> Any:
    # Imported lazily: supportcore.wiring must not load the openai SDK at import time.
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0)


async def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is not None:
        await close()


class OpenAITextToSpeech:
    def __init__(self, settings: Settings, *, client_factory: ClientFactory | None = None) -> None:
        self._settings = settings
        self._client_factory = client_factory or _default_client
        self._clients: dict[str, Any] = {}  # one client per api key, least recently used first; owned here

    async def _client(self, api_key: str) -> Any:
        client = self._clients.pop(api_key, None)
        if client is not None:
            self._clients[api_key] = client  # most recently used goes last
        else:
            client = self._client_factory(api_key)
            self._clients[api_key] = client
            while len(self._clients) > MAX_CLIENTS:
                oldest = self._clients.pop(next(iter(self._clients)))
                await _close(oldest)
        return client

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await _close(client)

    async def synthesize(self, text: str, config: TtsConfig) -> AsyncIterator[bytes]:
        if not config.api_key:
            raise TtsAuthError("missing api key")
        from openai import (
            APITimeoutError,
            AuthenticationError,
            OpenAIError,
            PermissionDeniedError,
            RateLimitError,
        )

        client = await self._client(config.api_key)
        resampler, framer = Resampler24kTo8k(), Framer()
        try:
            async with client.audio.speech.with_streaming_response.create(
                model=config.model, voice=config.voice, input=text, response_format="pcm"
            ) as response:
                async for chunk in response.iter_bytes(CHUNK_BYTES):
                    for frame in framer.feed(pcm16_to_mulaw(resampler.feed(chunk))):
                        yield frame
            tail = resampler.flush()
            for frame in framer.feed(pcm16_to_mulaw(tail)) if tail else []:
                yield frame
            last = framer.flush()
            if last is not None:
                yield last
        except (AuthenticationError, PermissionDeniedError):
            raise TtsAuthError("openai rejected the api key") from None
        except RateLimitError:
            raise TtsError("rate_limited") from None
        except (APITimeoutError, httpx.TimeoutException):
            raise TtsError("timeout") from None
        except (OpenAIError, httpx.HTTPError, OSError):
            raise TtsError("connection_failed") from None
