"""Phone-caller client for the e2e voice specs (stdlib + `websockets`), playing Twilio and the caller against apps/voice
running with the fake STT/TTS and the fake Realtime model (SUPPORT_ENV=test).

    uv run python apps/web/tests/e2e/call_tool.py call --to +15005550006 --from +15551230000 \
        --account-sid AC<32 hex> --auth-token T --say "text" [--say "text" ...] [--hangup-after-turn N] \
        [--realtime] [--expect-transfer] [--voice-url http://localhost:8002]

It signs and posts /twilio/voice/inbound, reads the stream URL and token from the TwiML, opens the Media Streams
WebSocket, sends `connected` and `start`, waits for the greeting (mark turn-0), then for each --say sends one
`FAKE-STT:` frame (`FAKE-RT:` with --realtime) and waits for the bot's reply (a turn mark, or `goodbye` when the bot
ends the call). With --realtime a turn ends at the first turn mark after new bot audio (a tool call's response ends
with a mark of its own and no audio). Every mark the server sends is echoed back, as Twilio does once the audio before
it has played. Finally it posts a signed `completed` status callback. Prints one JSON object:

    {"call_sid": "CA...", "spoken": [utterances decoded from FAKE-TTS / FAKE-RT-REPLY / FAKE-RT-SAY frames],
     "closed_by": "server" | "client"}

With --expect-transfer the server must close the stream (the bot hands the call to a person): the tool then posts the
signed `<Connect action>` (after-stream), as Twilio does, and the caller stays on the line (no status callback; see
`hangup`). The JSON gains "after_stream_url", "said" (the <Say> lines of that TwiML), "conference" (the room,
`transfer-{id}`) and "transfer_id".

Other sub-commands (each takes --account-sid, --auth-token and --voice-url):

    agent --transfer-id ID --attempt N --digits 1|none
        One agent leg of a live transfer. Posts the signed agent-answer. A browser leg joins the room at once; a phone
        leg is asked to press 1, so the tool posts agent-accept (with Digits=1, or no digit for `none`). A leg that
        joined is reported to the room's status callback (conference-status, participant-join) and stays up; a `none`
        leg that did not join ends with a signed agent-status (CallStatus=no-answer). Prints {"answer", "accept",
        "outcome", "said", "leg_sid", "join_status"} with outcome "joined" (the server took the room's join: HTTP 2xx)
        | "join_refused" (it did not) | "no_answer" | "taken". `join_status` is the conference-status HTTP status.
        The leg's CallSid is the one the server's create_call got from the fake telephony, read back from the voice
        app's test-only `GET /test/fake-telephony/calls` (by the leg's transfer id and attempt), so the server records
        the join as this agent leg's (`agent_joined_at`).
    hangup --call-sid CA... --to N --from N
        The caller hangs up: the signed `completed` status callback.
    after-stream --url URL --call-sid CA... --to N --from N
        Posts the `<Connect action>` again (Twilio retries it); prints {"said", "conference"}.
    callback-done --call-id ID --dial-status completed
        The `<Dial action>` of an outbound callback; prints {"said"}.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

import websockets

DEFAULT_VOICE_URL = "http://localhost:8002"
TIMEOUT = 30  # seconds to wait for any one bot reply
CLOSE_WAIT = 15  # seconds to wait for the server to hang up after the goodbye
STT_PREFIX = b"FAKE-STT:"
RT_PREFIX = b"FAKE-RT:"
# Each bot utterance starts with one of these (fake TTS; fake Realtime replies and fixed lines).
AUDIO_PREFIXES = (b"FAKE-TTS:", b"FAKE-RT-REPLY:", b"FAKE-RT-SAY:")


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """base64(HMAC-SHA1(token, url + each param's key and value in key order)) - Twilio's request signature."""
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def post_signed_url(url: str, auth_token: str, params: dict[str, str]) -> str:
    """POSTs `params` as Twilio does (form-encoded, signed over the full URL with its query); returns the body."""
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(params).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": twilio_signature(auth_token, url, params),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read().decode()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"POST {urllib.parse.urlsplit(url).path} failed: HTTP {error.code}") from None


def post_signed(voice_url: str, path: str, auth_token: str, params: dict[str, str]) -> str:
    return post_signed_url(voice_url.rstrip("/") + path, auth_token, params)


def post_signed_status(url: str, auth_token: str, params: dict[str, str]) -> int:
    """Like `post_signed_url`, but returns the HTTP status (an error status is returned, not raised)."""
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(params).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": twilio_signature(auth_token, url, params),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def placed_leg_sid(voice_url: str, transfer_id: str, attempt: int) -> str:
    """The sid the fake telephony returned for this transfer attempt's agent leg (the server places the leg after
    its commit, so it is polled for), from the voice app's test-only read-back of its create_call calls."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{voice_url.rstrip('/')}/test/fake-telephony/calls", timeout=TIMEOUT) as response:
            created = json.loads(response.read())["create_call"]
        for placed in reversed(created):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(placed["answer_url"]).query)
            if query.get("transfer_id") == [transfer_id] and query.get("attempt") == [str(attempt)] and placed["sid"]:
                return placed["sid"]
        time.sleep(0.2)
    raise SystemExit(f"no agent leg was placed for transfer {transfer_id} attempt {attempt}")


def parse_stream(twiml: str) -> tuple[str, str, str]:
    """(stream url, token, <Connect action> url) from the <Connect><Stream> TwiML."""
    root = ElementTree.fromstring(twiml)
    connect = root.find("./Connect")
    stream = root.find("./Connect/Stream")
    if connect is None or stream is None:
        raise SystemExit(f"no <Stream> in the TwiML: {twiml[:200]}")
    token = next((p.get("value") for p in stream.findall("Parameter") if p.get("name") == "token"), None)
    if not token or not stream.get("url"):
        raise SystemExit("the <Stream> has no url or token")
    return stream.get("url"), token, connect.get("action") or ""


def read_twiml(twiml: str) -> dict:
    """What a TwiML response does: its <Say> lines, the conference room it joins (or None), and its <Gather> action."""
    if not twiml.strip():
        return {"said": [], "conference": None, "gather_action": None}
    root = ElementTree.fromstring(twiml)
    room = root.find("./Dial/Conference")
    gather = root.find("./Gather")
    return {
        "said": [say.text or "" for say in root.iter("Say")],
        "conference": room.text if room is not None else None,
        "gather_action": gather.get("action") if gather is not None else None,
    }


def transfer_id_of(room: str | None) -> str | None:
    match = re.fullmatch(r"transfer-([0-9a-f-]{36})", room or "")
    return match.group(1) if match else None


class Bot:
    """What the server has sent so far: marks seen and the utterances decoded from its fake audio frames."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self._current = bytearray()
        self.marks: list[str] = []

    def media(self, frame: bytes) -> None:
        prefix = next((p for p in AUDIO_PREFIXES if frame.startswith(p)), None)
        if prefix is not None:
            self._flush()
            frame = frame[len(prefix) :]
        self._current += frame

    def _flush(self) -> None:
        if self._current:
            self.spoken.append(self._current.rstrip(b"\xff").decode("utf-8", "replace"))
            self._current = bytearray()

    def mark(self, name: str) -> None:
        self._flush()
        self.marks.append(name)


async def play_call(args: argparse.Namespace, stream_url: str, token: str, call_sid: str, bot: Bot) -> str:
    """Runs the caller's side of the stream; returns who ended it ("server" or "client")."""
    stream_sid = "MZ" + secrets.token_hex(16)
    async with websockets.connect(stream_url) as ws:
        marks: asyncio.Queue[str] = asyncio.Queue()
        closed = asyncio.Event()

        async def reader() -> None:
            try:
                async for raw in ws:
                    event = json.loads(raw)
                    kind = event.get("event")
                    if kind == "media":
                        bot.media(base64.b64decode(event["media"]["payload"]))
                    elif kind == "mark":
                        name = event["mark"]["name"]
                        bot.mark(name)
                        # Twilio echoes a mark once the audio before it has played.
                        await ws.send(json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}}))
                        marks.put_nowait(name)
            except websockets.ConnectionClosed:
                pass
            finally:
                closed.set()

        async def wait_mark(*names: str, any_turn: bool = False) -> str | None:
            """The first of `names` (or, with any_turn, any `turn-*` mark) to arrive; None once the server has closed
            the socket."""
            deadline = time.monotonic() + TIMEOUT
            while time.monotonic() < deadline:
                while not marks.empty():
                    name = marks.get_nowait()
                    if name in names or (any_turn and name.startswith("turn-")):
                        return name
                if closed.is_set():
                    return None
                await asyncio.sleep(0.02)
            raise SystemExit(f"timed out waiting for the bot ({'/'.join(names)})")

        async def realtime_reply() -> str | None:
            """The first turn mark after new bot audio (or `goodbye`, or None once closed)."""
            heard = len(bot.spoken)
            while True:
                name = await wait_mark("goodbye", any_turn=True)
                if name is None or name == "goodbye" or len(bot.spoken) > heard:
                    return name

        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "sequenceNumber": "1",
                    "streamSid": stream_sid,
                    "start": {
                        "accountSid": args.account_sid,
                        "streamSid": stream_sid,
                        "callSid": call_sid,
                        "tracks": ["inbound"],
                        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                        "customParameters": {"token": token},
                    },
                }
            )
        )
        read_task = asyncio.ensure_future(reader())
        closed_by = "client"
        prefix = RT_PREFIX if args.realtime else STT_PREFIX
        try:
            ended = await wait_mark("turn-0", "goodbye") is None
            for turn, text in enumerate(args.say, start=1):
                if ended:
                    break
                if args.hangup_after_turn is not None and turn > args.hangup_after_turn:
                    break
                await ws.send(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"track": "inbound", "payload": base64.b64encode(prefix + text.encode()).decode()},
                        }
                    )
                )
                reply = await realtime_reply() if args.realtime else await wait_mark(f"turn-{turn}", "goodbye")
                ended = reply is None or reply == "goodbye"
            if ended:  # the bot said goodbye (or the line dropped): let it hang up
                try:
                    await asyncio.wait_for(closed.wait(), CLOSE_WAIT)
                    closed_by = "server"
                except TimeoutError:
                    pass
            elif closed.is_set():
                closed_by = "server"
        finally:
            if not closed.is_set():
                with_stop = {"event": "stop", "streamSid": stream_sid, "stop": {"accountSid": args.account_sid, "callSid": call_sid}}
                try:
                    await ws.send(json.dumps(with_stop))
                except websockets.ConnectionClosed:
                    pass
            await ws.close()
            await read_task
    return closed_by


def caller_params(args: argparse.Namespace, call_sid: str) -> dict[str, str]:
    return {"AccountSid": args.account_sid, "CallSid": call_sid, "From": args.sender, "To": args.to}


def hang_up(args: argparse.Namespace, call_sid: str, duration: int = 1) -> None:
    post_signed(
        args.voice_url,
        "/twilio/voice/status",
        args.auth_token,
        {**caller_params(args, call_sid), "CallStatus": "completed", "CallDuration": str(max(1, duration))},
    )


def call(args: argparse.Namespace) -> None:
    call_sid = "CA" + secrets.token_hex(16)
    base = caller_params(args, call_sid)
    started = time.monotonic()
    twiml = post_signed(
        args.voice_url,
        "/twilio/voice/inbound",
        args.auth_token,
        {**base, "CallStatus": "ringing", "Direction": "inbound", "ApiVersion": "2010-04-01"},
    )
    stream_url, token, action_url = parse_stream(twiml)
    bot = Bot()
    closed_by = asyncio.run(play_call(args, stream_url, token, call_sid, bot))
    out: dict = {"call_sid": call_sid, "spoken": bot.spoken, "closed_by": closed_by}
    if args.expect_transfer:
        if closed_by != "server" or not action_url:
            raise SystemExit(f"expected the server to hand the call over (closed_by={closed_by})")
        # The stream is over: Twilio runs the <Connect action>, and the caller stays on the line.
        after = read_twiml(post_signed_url(action_url, args.auth_token, {**base, "CallStatus": "in-progress"}))
        out.update(
            after_stream_url=action_url,
            said=after["said"],
            conference=after["conference"],
            transfer_id=transfer_id_of(after["conference"]),
        )
    else:
        hang_up(args, call_sid, round(time.monotonic() - started))
    json.dump(out, sys.stdout)
    print()


def agent(args: argparse.Namespace) -> None:
    """One agent leg: answered (browser joins; phone presses `--digits` or nothing), then joined or ended unanswered."""
    leg_sid = placed_leg_sid(args.voice_url, args.transfer_id, args.attempt)
    query = urllib.parse.urlencode({"transfer_id": args.transfer_id, "attempt": args.attempt})
    leg = {"AccountSid": args.account_sid, "CallSid": leg_sid, "Direction": "outbound-api"}

    def post(endpoint: str, params: dict[str, str], url: str | None = None) -> str:
        target = url or f"{args.voice_url.rstrip('/')}/twilio/voice/{endpoint}?{query}"
        return post_signed_url(target, args.auth_token, {**leg, **params})

    answer = read_twiml(post("agent-answer", {"CallStatus": "in-progress"}))
    out: dict = {
        "answer": None,
        "accept": None,
        "outcome": None,
        "said": list(answer["said"]),
        "leg_sid": leg_sid,
        "join_status": None,
    }
    joined = answer["conference"]
    if joined:
        out["answer"] = "join"
    elif answer["gather_action"]:
        out["answer"] = "gather"
        digits = {"Digits": "1"} if args.digits == "1" else {}
        accept = read_twiml(post("agent-accept", {"CallStatus": "in-progress", **digits}, url=answer["gather_action"]))
        out["said"] += accept["said"]
        joined = accept["conference"]
        out["accept"] = "join" if joined else ("taken" if accept["said"] else "hangup")
    else:
        out["answer"] = "taken" if answer["said"] else "hangup"
    if joined:
        # The room's status callback (set by the caller's <Conference>): this leg is in the room.
        status_query = urllib.parse.urlencode({"transfer_id": args.transfer_id})
        out["join_status"] = post_signed_status(
            f"{args.voice_url.rstrip('/')}/twilio/voice/conference-status?{status_query}",
            args.auth_token,
            {
                "AccountSid": args.account_sid,
                "CallSid": leg_sid,
                "ConferenceSid": "CF" + secrets.token_hex(16),
                "FriendlyName": joined,
                "StatusCallbackEvent": "participant-join",
            },
        )
        out["outcome"] = "joined" if 200 <= out["join_status"] < 300 else "join_refused"
    elif args.digits == "none":
        post("agent-status", {"CallStatus": "no-answer"})
        out["outcome"] = "no_answer"
    else:
        out["outcome"] = "taken"
    json.dump(out, sys.stdout)
    print()


def hangup(args: argparse.Namespace) -> None:
    hang_up(args, args.call_sid)
    json.dump({"call_sid": args.call_sid, "status": "completed"}, sys.stdout)
    print()


def after_stream(args: argparse.Namespace) -> None:
    params = {**caller_params(args, args.call_sid), "CallStatus": "in-progress"}
    after = read_twiml(post_signed_url(args.url, args.auth_token, params))
    json.dump({"said": after["said"], "conference": after["conference"]}, sys.stdout)
    print()


def callback_done(args: argparse.Namespace) -> None:
    params = {
        "AccountSid": args.account_sid,
        "CallSid": "CA" + secrets.token_hex(16),
        "CallStatus": "in-progress",
        "DialCallStatus": args.dial_status,
    }
    path = f"/twilio/voice/callback-done?{urllib.parse.urlencode({'call_id': args.call_id})}"
    after = read_twiml(post_signed(args.voice_url, path, args.auth_token, params))
    json.dump({"said": after["said"]}, sys.stdout)
    print()


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--account-sid", required=True, help="the Twilio AccountSid the number belongs to (AC + 32 hex)")
    common.add_argument("--auth-token", required=True)
    common.add_argument("--voice-url", default=DEFAULT_VOICE_URL, help="SUPPORT_VOICE_PUBLIC_URL of the voice service")
    caller = argparse.ArgumentParser(add_help=False)
    caller.add_argument("--to", required=True, help="the connected number (E.164)")
    caller.add_argument("--from", dest="sender", required=True, help="the caller's number (E.164)")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("call", parents=[common, caller], help="place one scripted phone call")
    c.add_argument("--say", action="append", default=[], help="one caller utterance; repeat for more")
    c.add_argument("--hangup-after-turn", type=int, help="hang up after the bot's reply to this utterance (1-based)")
    c.add_argument("--realtime", action="store_true", help="speech-to-speech: FAKE-RT: frames to the fake Realtime model")
    c.add_argument("--expect-transfer", action="store_true", help="the bot hands over: post the after-stream, stay on")
    c.set_defaults(run=call)
    a = sub.add_parser("agent", parents=[common], help="answer one agent leg of a live transfer")
    a.add_argument("--transfer-id", required=True)
    a.add_argument("--attempt", required=True, type=int)
    a.add_argument("--digits", required=True, choices=["1", "none"], help="the key a phone leg presses, or none")
    a.set_defaults(run=agent)
    h = sub.add_parser("hangup", parents=[common, caller], help="the caller hangs up (completed status callback)")
    h.add_argument("--call-sid", required=True)
    h.set_defaults(run=hangup)
    s = sub.add_parser("after-stream", parents=[common, caller], help="post the <Connect action> again")
    s.add_argument("--url", required=True)
    s.add_argument("--call-sid", required=True)
    s.set_defaults(run=after_stream)
    d = sub.add_parser("callback-done", parents=[common], help="the <Dial action> of an outbound callback")
    d.add_argument("--call-id", required=True)
    d.add_argument("--dial-status", required=True)
    d.set_defaults(run=callback_done)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"AC[0-9a-fA-F]{32}", args.account_sid):
        parser.error("--account-sid must be AC followed by 32 hex digits")
    args.run(args)


if __name__ == "__main__":
    main()
