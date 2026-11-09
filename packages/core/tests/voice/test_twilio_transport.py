"""TwilioMediaTransport over a scripted stand-in for the ASGI WebSocket."""

import asyncio
import base64
import json

import pytest
from supportcore.voice import transport as transport_module
from supportcore.voice.transport import (
    MESSAGE_TOO_BIG,
    AudioIn,
    MarkPlayed,
    Stop,
    TransportClosed,
    TwilioMediaTransport,
)

SID = "MZ" + "1" * 32


class Socket:
    def __init__(self, *messages) -> None:
        self.inbound: asyncio.Queue = asyncio.Queue()
        for message in messages:
            self.push(message)
        self.sent: list[dict] = []
        self.closed: list[int] = []

    def push(self, message) -> None:
        if isinstance(message, dict):
            message = json.dumps(message)
        self.inbound.put_nowait({"type": "websocket.receive", "text": message})

    def hang_up(self) -> None:
        self.inbound.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def receive(self):
        return await self.inbound.get()

    async def send_text(self, data: str) -> None:
        if self.closed:
            raise RuntimeError("closed")
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


def start(token="tok"):
    return {"event": "start", "streamSid": SID, "start": {"streamSid": SID, "callSid": "CA1", "customParameters": {"token": token}}}


def media(data: bytes):
    return {"event": "media", "streamSid": SID, "media": {"track": "inbound", "payload": base64.b64encode(data).decode()}}


async def collect(transport):
    return [event async for event in transport.events()]


async def test_start_then_events_and_outbound_json():
    socket = Socket({"event": "connected"}, "not json", start(), media(b"abc"), {"event": "dtmf"},
                    {"event": "mark", "mark": {"name": "turn-0"}}, {"event": "stop"}, media(b"after stop"))
    transport = TwilioMediaTransport(socket)
    started = await transport.wait_for_start(1)
    assert started is not None and started.stream_sid == SID and started.parameters == {"token": "tok"}
    assert "tok" not in repr(started)
    assert await collect(transport) == [AudioIn(b"abc"), MarkPlayed("turn-0"), Stop()]
    await transport.send_audio(b"\x7f" * 160)
    await transport.send_mark("turn-1")
    await transport.clear()
    assert socket.sent == [
        {"event": "media", "streamSid": SID, "media": {"payload": base64.b64encode(b"\x7f" * 160).decode()}},
        {"event": "mark", "streamSid": SID, "mark": {"name": "turn-1"}},
        {"event": "clear", "streamSid": SID},
    ]
    await transport.close()
    await transport.close()
    assert socket.closed == [1000]
    with pytest.raises(TransportClosed):
        await transport.send_audio(b"x")


async def test_no_start_in_time_or_disconnect_gives_none():
    quiet = TwilioMediaTransport(Socket({"event": "connected"}))
    assert await quiet.wait_for_start(0.05) is None
    gone = Socket({"event": "connected"})
    gone.hang_up()
    assert await TwilioMediaTransport(gone).wait_for_start(1) is None


async def test_oversized_message_and_frame_close_1009():
    big = Socket("x" * (64 * 1024 + 1))
    assert await TwilioMediaTransport(big).wait_for_start(1) is None
    assert big.closed == [MESSAGE_TOO_BIG]

    socket = Socket(start(), media(b"\x7f" * 4096), media(b"\x7f" * 4097), media(b"never read"))
    transport = TwilioMediaTransport(socket)
    assert await transport.wait_for_start(1) is not None
    assert await collect(transport) == [AudioIn(b"\x7f" * 4096)]
    assert socket.closed == [MESSAGE_TOO_BIG]


async def test_disconnect_ends_events_and_sends_raise():
    socket = Socket(start(), media(b"a"))
    socket.hang_up()
    transport = TwilioMediaTransport(socket)
    await transport.wait_for_start(1)
    assert await collect(transport) == [AudioIn(b"a")]
    with pytest.raises(TransportClosed):
        await transport.send_mark("x")


async def test_deeply_nested_json_is_ignored_as_malformed():
    socket = Socket("[" * 15000, start(), "{" * 15000 + "}" * 15000, media(b"ok"), {"event": "stop"})
    transport = TwilioMediaTransport(socket)
    assert await transport.wait_for_start(1) is not None
    assert await collect(transport) == [AudioIn(b"ok"), Stop()]


async def test_a_send_the_peer_does_not_take_in_time_closes(monkeypatch):
    monkeypatch.setattr(transport_module, "SEND_TIMEOUT_SECONDS", 0.05)

    class Stalled(Socket):
        async def send_text(self, data: str) -> None:
            await asyncio.sleep(3600)

    socket = Stalled(start())
    transport = TwilioMediaTransport(socket)
    await transport.wait_for_start(1)
    with pytest.raises(TransportClosed):
        await transport.send_audio(b"\x7f" * 160)
    with pytest.raises(TransportClosed):
        await transport.clear()
