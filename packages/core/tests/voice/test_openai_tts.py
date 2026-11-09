import asyncio

import httpx
import openai
import pytest
from supportcore.config import Settings
from supportcore.voice.tts.openai_tts import MAX_CLIENTS, OpenAITextToSpeech
from supportcore.voice.tts.ports import TtsAuthError, TtsConfig, TtsError

pytestmark = pytest.mark.asyncio

CONFIG = TtsConfig(provider="openai", model="gpt-4o-mini-tts", voice="coral", language="en", api_key="sk-x")


class _Response:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_bytes(self, chunk_size=None):
        for chunk in self._chunks:
            yield chunk


class _Stream:
    exits = 0

    def __init__(self, chunks, error):
        self._chunks, self._error = chunks, error

    async def __aenter__(self):
        if self._error:
            raise self._error
        return _Response(self._chunks)

    async def __aexit__(self, *exc):
        _Stream.exits += 1
        return False


class StubClient:
    def __init__(self, chunks=(), error=None):
        self.calls = []
        self._chunks, self._error = chunks, error
        self.audio = self
        self.speech = self
        self.with_streaming_response = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Stream(self._chunks, self._error)


class _Closable(StubClient):
    closed = 0

    async def close(self):
        self.closed += 1


def _tts(client, keys=None):
    def factory(api_key):
        if keys is not None:
            keys.append(api_key)
        return client

    return OpenAITextToSpeech(Settings(_env_file=None, env="test"), client_factory=factory)


async def _collect(tts, text="hi"):
    return [f async for f in tts.synthesize(text, CONFIG)]


async def test_tts_resamples_and_frames():
    pcm = bytes(48_000)  # 1 s of 24 kHz silence -> 8 000 8 kHz samples -> 8 000 µ-law bytes
    client = StubClient([pcm[:10_001], pcm[10_001:30_000], pcm[30_000:]])  # odd chunking
    keys = []
    frames = await _collect(_tts(client, keys), "hello")
    assert keys == ["sk-x"]
    assert client.calls == [
        {"model": "gpt-4o-mini-tts", "voice": "coral", "input": "hello", "response_format": "pcm"}
    ]
    assert all(len(f) == 160 for f in frames)
    assert len(frames) == 50
    assert b"".join(frames) == b"\xff" * 8000  # silence in µ-law


async def test_tts_last_frame_is_padded():
    frames = await _collect(_tts(StubClient([bytes(12_300)])))  # 2 050 µ-law bytes -> 12 frames + 130 bytes
    assert len(frames) == 13
    assert all(len(f) == 160 for f in frames)
    assert frames[-1] == b"\xff" * 160


async def _raises(error):
    with pytest.raises((TtsAuthError, TtsError)) as err:
        await _collect(_tts(StubClient(error=error)))
    return err.value


def _status_error(cls, status):
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    return cls("secret-detail sk-x", response=httpx.Response(status, request=request), body=None)


async def test_tts_auth_error_mapped():
    assert isinstance(await _raises(_status_error(openai.AuthenticationError, 401)), TtsAuthError)


async def test_tts_rate_limit_mapped():
    err = await _raises(_status_error(openai.RateLimitError, 429))
    assert isinstance(err, TtsError) and err.error_type == "rate_limited"
    assert "sk-x" not in str(err)


async def test_tts_timeout_and_other_errors_mapped():
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    assert (await _raises(openai.APITimeoutError(request=request))).error_type == "timeout"
    assert (await _raises(openai.APIConnectionError(request=request))).error_type == "connection_failed"
    assert (await _raises(_status_error(openai.InternalServerError, 500))).error_type == "connection_failed"


async def test_default_client_has_no_retries_and_15s_timeout():
    from supportcore.voice.tts.openai_tts import _default_client

    client = _default_client("sk-x")
    assert client.max_retries == 0
    assert client.timeout == 15


async def test_permission_denied_mapped_to_auth_error():
    assert isinstance(await _raises(_status_error(openai.PermissionDeniedError, 403)), TtsAuthError)


async def test_stream_closed_when_consumer_task_is_cancelled():
    _Stream.exits = 0
    started = asyncio.Event()

    class Slow(StubClient):
        def create(self, **kwargs):

            class S(_Stream):
                async def __aenter__(self):
                    class R:
                        async def iter_bytes(self, n=None):
                            yield bytes(9600)
                            started.set()
                            await asyncio.sleep(10)

                    return R()

            return S([], None)

    async def consume():
        async for _ in _tts(Slow()).synthesize("hi", CONFIG):
            pass

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _Stream.exits == 1


async def test_stream_closed_when_consumer_aclose_mid_stream():
    _Stream.exits = 0
    gen = _tts(StubClient([bytes(48_000)])).synthesize("hi", CONFIG)
    await gen.__anext__()
    await gen.aclose()
    assert _Stream.exits == 1


async def test_client_reused_per_key_and_closed_by_aclose():
    built = []

    def factory(key):
        built.append(key)
        return _Closable([bytes(960)])

    tts = OpenAITextToSpeech(Settings(_env_file=None, env="test"), client_factory=factory)
    await _collect(tts)
    await _collect(tts)
    assert built == ["sk-x"]
    client = tts._clients["sk-x"]
    await tts.aclose()
    assert client.closed == 1


async def test_client_cache_is_bounded_and_evicts_least_recently_used():
    made = []

    def factory(key):
        made.append(_Closable([bytes(960)]))
        return made[-1]

    tts = OpenAITextToSpeech(Settings(_env_file=None, env="test"), client_factory=factory)
    def cfg(i):
        return TtsConfig(provider="openai", model="m", voice="v", language="en", api_key=f"k{i}")

    for i in range(MAX_CLIENTS):
        _ = [f async for f in tts.synthesize("hi", cfg(i))]
    _ = [f async for f in tts.synthesize("hi", cfg(0))]  # k0 used again: k1 is now the least recently used
    _ = [f async for f in tts.synthesize("hi", cfg(MAX_CLIENTS))]
    assert len(tts._clients) == MAX_CLIENTS
    assert made[0].closed == 0
    assert made[1].closed == 1
