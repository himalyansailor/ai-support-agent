"""In-memory TextToSpeech for tests (registered only when env == "test").

Yields b"FAKE-TTS:" + the text, in 160-byte frames (the last padded with 0xFF). Text containing "FAIL" raises
TtsAuthError.
"""

from collections.abc import AsyncIterator

from supportcore.voice.tts.ports import TtsAuthError, TtsConfig

FRAME_BYTES = 160


class FakeTextToSpeech:
    def __init__(self) -> None:
        self.requests: list[tuple[str, TtsConfig]] = []

    async def synthesize(self, text: str, config: TtsConfig) -> AsyncIterator[bytes]:
        self.requests.append((text, config))
        if "FAIL" in text:
            raise TtsAuthError("fake tts auth failure")
        audio = b"FAKE-TTS:" + text.encode()
        for start in range(0, len(audio), FRAME_BYTES):
            yield audio[start : start + FRAME_BYTES].ljust(FRAME_BYTES, b"\xff")
