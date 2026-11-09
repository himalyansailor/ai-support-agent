import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { connectInbox } from "@/lib/inbox-socket";

class FakeWebSocket {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.closed = false;
    FakeWebSocket.instances.push(this);
  }
  send(data) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.closed = true;
  }
  open() {
    this.onopen?.();
  }
  receive(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
  drop(code = 1006) {
    this.onclose?.({ code });
  }
}

const COUNTS = { mine: 1, unassigned: 2, bot: 3 };
const last = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];

function setup(overrides = {}) {
  const events = [];
  const client = { messages: vi.fn(async () => []) };
  let token = "tok-1";
  const conn = connectInbox({
    apiUrl: "http://api.test",
    orgId: "org1",
    getToken: () => token,
    onEvent: (e) => events.push(e),
    client,
    WebSocketImpl: FakeWebSocket,
    ...overrides,
  });
  return { conn, events, client, setToken: (t) => (token = t) };
}

function ready(ws = last()) {
  ws.open();
  ws.receive({ type: "ready", membership_id: "m1", counts: COUNTS });
}

describe("connectInbox", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
  });
  afterEach(() => vi.useRealTimers());

  it("auth first", () => {
    const { events } = setup();
    const ws = last();
    expect(ws.url).toBe("ws://api.test/inbox/v1/ws");
    ws.open();
    expect(ws.sent).toEqual([{ type: "auth", token: "tok-1", org_id: "org1" }]);
    ws.receive({ type: "ready", membership_id: "m1", counts: COUNTS });
    expect(events.at(-1)).toEqual({ type: "ready", membership_id: "m1", counts: COUNTS });
  });

  it("waits for an async token before sending auth", async () => {
    setup({ getToken: async () => "tok-async" });
    const ws = last();
    ws.open();
    expect(ws.sent).toEqual([]);
    await vi.advanceTimersByTimeAsync(0);
    expect(ws.sent).toEqual([{ type: "auth", token: "tok-async", org_id: "org1" }]);
  });

  it("ping interval", () => {
    setup();
    const ws = last();
    ready(ws);
    const pings = () => ws.sent.filter((f) => f.type === "ping").length;
    vi.advanceTimersByTime(19_999);
    expect(pings()).toBe(0);
    vi.advanceTimersByTime(1);
    expect(pings()).toBe(1);
    vi.advanceTimersByTime(20_000);
    expect(pings()).toBe(2);
    ws.drop(1011); // a dead socket stops pinging
    vi.advanceTimersByTime(500);
    vi.advanceTimersByTime(20_000);
    expect(pings()).toBe(2);
  });

  it("rewatch after reconnect, re-authenticating with the current token", () => {
    const { conn, setToken } = setup();
    conn.watch("c1"); // before ready: queued
    let ws = last();
    ws.open();
    expect(ws.sent).toEqual([{ type: "auth", token: "tok-1", org_id: "org1" }]);
    ws.receive({ type: "ready", membership_id: "m1", counts: COUNTS });
    expect(ws.sent.at(-1)).toEqual({ type: "watch", conversation_id: "c1" });

    setToken("tok-2");
    ws.drop(1011);
    vi.advanceTimersByTime(1000);
    ws = last();
    expect(FakeWebSocket.instances).toHaveLength(2);
    ready(ws);
    expect(ws.sent).toEqual([
      { type: "auth", token: "tok-2", org_id: "org1" },
      { type: "watch", conversation_id: "c1" },
    ]);

    conn.unwatch();
    vi.advanceTimersByTime(250); // watch changes are coalesced
    expect(ws.sent.at(-1)).toEqual({ type: "unwatch" });
    ws.drop(1012);
    vi.advanceTimersByTime(1000);
    ready(last());
    expect(last().sent.filter((f) => f.type === "watch")).toEqual([]);
  });

  it("gap refill: every watching ack refetches the watched conversation, and a reconnect announces itself", async () => {
    const { conn, events, client } = setup();
    conn.watch("c1");
    let ws = last();
    ready(ws);
    client.messages.mockResolvedValueOnce([{ id: "m1" }]);
    ws.receive({ type: "watching", conversation_id: "c1" });
    await vi.advanceTimersByTimeAsync(0);
    // the first open too: whatever arrived between the page's detail read and the subscription
    expect(client.messages).toHaveBeenCalledTimes(1);
    expect(events.at(-1)).toEqual({ type: "messages", conversation_id: "c1", messages: [{ id: "m1" }] });

    ws.drop(1013);
    vi.advanceTimersByTime(1000);
    ws = last();
    client.messages.mockResolvedValueOnce([{ id: "m9" }]);
    ready(ws);
    expect(events.filter((e) => e.type === "reconnected")).toHaveLength(1);
    expect(events.filter((e) => e.type === "ready").at(-1).counts).toEqual(COUNTS); // counts re-read
    ws.receive({ type: "watching", conversation_id: "c1" });
    await vi.advanceTimersByTimeAsync(0);
    expect(client.messages).toHaveBeenCalledTimes(2);
    expect(client.messages).toHaveBeenLastCalledWith("c1");
    expect(events.at(-1)).toEqual({ type: "messages", conversation_id: "c1", messages: [{ id: "m9" }] });

    // an ack for a conversation no longer watched is not refilled
    ws.receive({ type: "watching", conversation_id: "c-old" });
    await vi.advanceTimersByTimeAsync(0);
    expect(client.messages).toHaveBeenCalledTimes(2);
  });

  it("coalesces rapid watch changes: 50 switches send at most 2 frames and the last id wins", () => {
    const { conn } = setup();
    const ws = last();
    ready(ws);
    vi.advanceTimersByTime(1000);
    for (let i = 0; i < 50; i += 1) {
      conn.unwatch(); // what a pane does on unmount, before the next one watches
      conn.watch(`c${i}`);
      vi.advanceTimersByTime(10);
    }
    vi.advanceTimersByTime(1000);
    const frames = ws.sent.filter((f) => f.type === "watch" || f.type === "unwatch");
    expect(frames.length).toBeLessThanOrEqual(2);
    expect(frames.at(-1)).toEqual({ type: "watch", conversation_id: "c49" });
    expect(frames.some((f) => f.type === "unwatch")).toBe(false);
  });

  it("drops a refill for a conversation that is no longer watched", async () => {
    const { conn, events, client } = setup();
    conn.watch("c1");
    ready();
    last().drop(1011);
    vi.advanceTimersByTime(1000);
    let resolve;
    client.messages.mockReturnValueOnce(new Promise((r) => (resolve = r)));
    ready();
    last().receive({ type: "watching", conversation_id: "c1" });
    conn.watch("c2");
    resolve([{ id: "m1" }]);
    await vi.advanceTimersByTimeAsync(0);
    expect(events.filter((e) => e.type === "messages")).toEqual([]);
  });

  it("backs off 1/2/4/8 s, capped at 30 s, and resets after ready", () => {
    setup();
    const waits = [1000, 2000, 4000, 8000, 16000, 30000, 30000];
    for (const w of waits) {
      const n = FakeWebSocket.instances.length;
      last().drop(1006);
      vi.advanceTimersByTime(w - 1);
      expect(FakeWebSocket.instances).toHaveLength(n);
      vi.advanceTimersByTime(1);
      expect(FakeWebSocket.instances).toHaveLength(n + 1);
    }
    ready();
    const n = FakeWebSocket.instances.length;
    last().drop(1011);
    vi.advanceTimersByTime(1000);
    expect(FakeWebSocket.instances).toHaveLength(n + 1);
  });

  it("terminal codes: no reconnect on 4401, 4403 or 1008, nor after close()", () => {
    for (const code of [4401, 4403, 1008]) {
      FakeWebSocket.instances = [];
      const { events } = setup();
      ready();
      last().drop(code);
      vi.advanceTimersByTime(60_000);
      expect(FakeWebSocket.instances).toHaveLength(1);
      expect(events.at(-1)).toEqual({ type: "closed", code });
    }
    FakeWebSocket.instances = [];
    const { conn } = setup();
    conn.close();
    expect(last().closed).toBe(true);
    last().drop(1000);
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("keeps the newest upsert version per conversation and the newest presence per member", () => {
    const { events } = setup();
    const ws = last();
    ready(ws);
    ws.receive({ type: "conversation.upsert", conversation: { id: "c1", version: 2 } });
    ws.receive({ type: "conversation.upsert", conversation: { id: "c1", version: 1 } });
    ws.receive({ type: "conversation.upsert", conversation: { id: "c1", version: 2 } });
    ws.receive({ type: "conversation.upsert", conversation: { id: "c1", version: 3 } });
    ws.receive({ type: "conversation.upsert", conversation: { id: "c2", version: 1 } });
    const ups = events.filter((e) => e.type === "conversation.upsert").map((e) => e.conversation);
    expect(ups).toEqual([
      { id: "c1", version: 2 },
      { id: "c1", version: 3 },
      { id: "c2", version: 1 },
    ]);

    ws.receive({ type: "presence", membership_id: "m2", agent_status: "away", at: 20 });
    ws.receive({ type: "presence", membership_id: "m2", agent_status: "online", at: 10 });
    ws.receive({ type: "presence", membership_id: "m2", agent_status: "offline", at: 30 });
    const pres = events.filter((e) => e.type === "presence").map((e) => e.agent_status);
    expect(pres).toEqual(["away", "offline"]);
  });

  it("relays watched-conversation events and notifications, but not pong", () => {
    const { events } = setup();
    const ws = last();
    ready(ws);
    ws.receive({ type: "pong" });
    ws.receive({ type: "bot_token", delta: "Hi", conversation_id: "c1" });
    ws.receive({ type: "notification", notification: { id: "n1" } });
    expect(events.slice(-2).map((e) => e.type)).toEqual(["bot_token", "notification"]);
  });
});
