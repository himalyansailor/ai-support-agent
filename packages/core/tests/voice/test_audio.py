import numpy as np
from supportcore.voice.audio import (
    FRAME_BYTES,
    Framer,
    Resampler24kTo8k,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)


def _sine(freq: float, rate: int, n: int, amp: float) -> bytes:
    t = np.arange(n) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype("<i2").tobytes()


def test_mulaw_round_trip_within_quantisation():
    pcm = _sine(1000, 8000, 800, 8000)
    back = mulaw_to_pcm16(pcm16_to_mulaw(pcm))
    x = np.frombuffer(pcm, dtype="<i2").astype(int)
    y = np.frombuffer(back, dtype="<i2").astype(int)
    assert len(x) == len(y)
    assert (np.abs(x - y) <= 0.02 * np.abs(x) + 16).all()


def test_mulaw_known_values():
    assert pcm16_to_mulaw(b"\x00\x00") == b"\xff"
    assert pcm16_to_mulaw(np.array([32767], dtype="<i2").tobytes()) == b"\x80"
    assert pcm16_to_mulaw(np.array([-32768], dtype="<i2").tobytes()) == b"\x00"


def test_resampler_length_and_streaming_equivalence():
    pcm = _sine(440, 24000, 24000, 6000)
    whole = Resampler24kTo8k()
    out_whole = whole.feed(pcm) + whole.flush()
    assert len(out_whole) == 8000 * 2
    chunked = Resampler24kTo8k()
    out = b""
    for i in range(0, len(pcm), 7):
        out += chunked.feed(pcm[i : i + 7])
    out += chunked.flush()
    assert out == out_whole


def test_resampler_attenuates_above_4khz():
    n = 24000
    tone = np.frombuffer(_sine(6000, 24000, n, 10000), dtype="<i2")
    ref = np.frombuffer(_sine(1000, 24000, n, 10000), dtype="<i2")

    def rms(pcm: bytes) -> float:
        a = np.frombuffer(pcm, dtype="<i2").astype(float)[200:]
        return float(np.sqrt(np.mean(a**2)))

    hi = rms(Resampler24kTo8k().feed(tone.tobytes()))
    lo = rms(Resampler24kTo8k().feed(ref.tobytes()))
    assert 20 * np.log10(lo / hi) >= 20


def test_framer_pads_last_frame():
    framer = Framer()
    frames = framer.feed(b"\x01" * 250)
    assert frames == [b"\x01" * FRAME_BYTES]
    assert framer.feed(b"\x02" * 71) == [b"\x01" * 90 + b"\x02" * 70]
    last = framer.flush()
    assert last == b"\x02" + b"\xff" * (FRAME_BYTES - 1)
    assert framer.flush() is None
