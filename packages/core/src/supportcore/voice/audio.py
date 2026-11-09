"""G.711 µ-law conversion, a 24 kHz -> 8 kHz resampler and a 20 ms framer."""

import numpy as np

FRAME_BYTES = 160  # 20 ms of 8 kHz µ-law
_BIAS = 0x84
_CLIP = 32635
_SILENCE = 0xFF

# exponent for (magnitude >> 7), which is in 1..255
_EXP_TABLE = np.array([0] + [int(v).bit_length() - 1 for v in range(1, 256)], dtype=np.int32)


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """16-bit little-endian PCM -> µ-law bytes (a trailing odd byte is ignored)."""
    samples = np.frombuffer(pcm, dtype="<i2", count=len(pcm) // 2).astype(np.int32)
    sign = np.where(samples < 0, 0x80, 0)
    mag = np.minimum(np.abs(samples), _CLIP) + _BIAS
    exponent = _EXP_TABLE[mag >> 7]
    mantissa = (mag >> (exponent + 3)) & 0x0F
    return ((~(sign | (exponent << 4) | mantissa)) & 0xFF).astype(np.uint8).tobytes()


def mulaw_to_pcm16(data: bytes) -> bytes:
    """µ-law bytes -> 16-bit little-endian PCM."""
    u = (~np.frombuffer(data, dtype=np.uint8).astype(np.int32)) & 0xFF
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    mag = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return np.where(u & 0x80, -mag, mag).astype("<i2").tobytes()


_TAPS = 31
_DECIMATION = 3


def _lowpass_kernel() -> np.ndarray:
    cutoff = 3400 / 24000  # cycles per sample
    n = np.arange(_TAPS) - (_TAPS - 1) / 2
    kernel = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(_TAPS)
    return kernel / kernel.sum()


class Resampler24kTo8k:
    """Streaming 16-bit PCM 24 kHz -> 8 kHz: low-pass at 3.4 kHz, keep every 3rd sample.

    Keeps a trailing odd byte, the filter history and the decimation phase between calls,
    so any chunking of the input gives the same output.
    """

    def __init__(self) -> None:
        self._kernel = _lowpass_kernel()
        self._history = np.zeros(_TAPS - 1, dtype=np.float64)
        self._pending = b""
        self._phase = 0  # samples to skip before the next kept one

    def feed(self, pcm16_24k: bytes) -> bytes:
        data = self._pending + pcm16_24k
        usable = len(data) - (len(data) % 2)
        self._pending = data[usable:]
        if usable == 0:
            return b""
        new = np.frombuffer(data[:usable], dtype="<i2").astype(np.float64)
        stream = np.concatenate([self._history, new])
        filtered = np.convolve(stream, self._kernel, mode="valid")  # one output per new sample
        self._history = stream[-(_TAPS - 1) :]
        kept = filtered[self._phase :: _DECIMATION]
        self._phase = (self._phase - len(filtered)) % _DECIMATION
        return np.clip(np.rint(kept), -32768, 32767).astype("<i2").tobytes()

    def flush(self) -> bytes:
        """End of stream: a dangling odd byte is not a sample, so there is nothing to emit."""
        self._pending = b""
        return b""


class Framer:
    """Cuts a µ-law byte stream into whole 160-byte frames."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        whole = len(self._buf) - (len(self._buf) % FRAME_BYTES)
        frames = [self._buf[i : i + FRAME_BYTES] for i in range(0, whole, FRAME_BYTES)]
        self._buf = self._buf[whole:]
        return frames

    def flush(self) -> bytes | None:
        if not self._buf:
            return None
        frame = self._buf + bytes([_SILENCE]) * (FRAME_BYTES - len(self._buf))
        self._buf = b""
        return frame
