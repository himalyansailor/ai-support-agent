import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { connectConversation } from "@/lib/widget-socket";

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
  // test helpers
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

const msg = (id, extra = {}) => ({
  id,
  role: "bot",
  text: "hi",
  citations: [],
  client_id: null,
  created_at: `2026-01-01T00:00:0${id}Z`,
  ...extra,
});

function setup(overrides = {}) {
  const events = [];
  const client = { messages: vi.fn(async () => []) };
  const conn = connectConversation({
    apiUrl: "http://api.test",
    conversationId: "c1",
    token: "tok",
    client,
    WebSocketImpl: FakeWebSocket,
    onEvent: (e) => events.push(e),
    ...overrides,
  });
  return { conn, events, client };
}

const last = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];

describe("connectConversation", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
  });
  afterEach(() => vi.useRealTimers());

  it("sends auth first", () => {
    setup();
    const ws = last();
    expect(ws.url).toBe("ws://api.test/widget/v1/conversations/c1/ws");
    ws.open();
    expect(ws.sent).toEqual([{ type: "auth", token: "tok" }]);
  });

  it("reconnects with backoff and refetches gap", async () => {
    const { events, client } = setup();
    let ws = last();
    ws.open();
    ws.receive({ type: "ready", status: "bot" });
    ws.receive({ type: "message", message: msg(1) });
    ws.drop(1011);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    // second failure waits 2 s
    ws = last();
    ws.drop(1006);
    vi.advanceTimersByTime(1999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);

    client.messages.mockResolvedValueOnce([msg(1), msg(2)]);
    ws = last();
    ws.open();
    ws.receive({ type: "ready", status: "bot" });
    await vi.advanceTimersByTimeAsync(0);
    expect(client.messages).toHaveBeenCalledWith("c1", "2026-01-01T00:00:01Z");
    const ids = events.filter((e) => e.type === "message").map((e) => e.message.id);
    expect(ids).toEqual([1, 2]);
  });

  it("refetches the gap on reconnect even when nothing was seen before the drop", async () => {
    const { client } = setup();
    let ws = last();
    ws.open();
    ws.receive({ type: "ready", status: "bot" });
    ws.drop(1011);
    vi.advanceTimersByTime(1000);
    ws = last();
    ws.open();
    ws.receive({ type: "ready", status: "bot" });
    await vi.advanceTimersByTimeAsync(0);
    expect(client.messages).toHaveBeenCalledWith("c1", undefined);
  });

  it("caps backoff at 30 s", () => {
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
  });

  it("does not reconnect on 4401/4403/4404 or after close()", () => {
    for (const code of [4401, 4403, 4404]) {
      FakeWebSocket.instances = [];
      const { events } = setup();
      last().drop(code);
      vi.advanceTimersByTime(60000);
      expect(FakeWebSocket.instances).toHaveLength(1);
      expect(events.at(-1)).toEqual({ type: "closed", code });
    }
    FakeWebSocket.instances = [];
    const { conn } = setup();
    conn.close();
    last().drop(1000);
    vi.advanceTimersByTime(60000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("dedupes messages by id", () => {
    const { events } = setup();
    const ws = last();
    ws.open();
    ws.receive({ type: "ready", status: "bot" });
    ws.receive({ type: "message", message: msg(1) });
    ws.receive({ type: "message", message: msg(1) });
    ws.receive({ type: "message", message: msg(2) });
    expect(events.filter((e) => e.type === "message")).toHaveLength(2);
  });

  it("resends unsent with same client_id", () => {
    const { conn } = setup();
    let ws = last();
    ws.open();
    ws.receive({ type: "ready", status: "bot" });
    const cid = conn.send("hello");
    expect(ws.sent.at(-1)).toEqual({ type: "message", text: "hello", client_id: cid });
    ws.drop(1011);
    vi.advanceTimersByTime(1000);
    ws = last();
    ws.open();
    ws.receive({ type: "ready", status: "bot" });
    const resent = ws.sent.filter((f) => f.type === "message");
    expect(resent).toEqual([{ type: "message", text: "hello", client_id: cid }]);
    // once echoed it is no longer retried
    ws.receive({ type: "message", message: msg(5, { role: "customer", client_id: cid }) });
    ws.drop(1011);
    vi.advanceTimersByTime(2000);
    ws = last();
    ws.open();
    ws.receive({ type: "ready", status: "bot" });
    expect(ws.sent.filter((f) => f.type === "message")).toHaveLength(0);
  });

  it("talkToHuman sends the frame", () => {
    const { conn } = setup();
    const ws = last();
    ws.open();
    ws.receive({ type: "ready", status: "bot" });
    conn.talkToHuman();
    expect(ws.sent.at(-1)).toEqual({ type: "talk_to_human" });
  });

  it("queues talkToHuman during a reconnect and sends it once ready", () => {
    const { conn } = setup();
    let ws = last();
    ws.open();
    ws.receive({ type: "ready", status: "bot" });
    ws.drop(1011);
    conn.talkToHuman(); // clicked while reconnecting
    expect(ws.sent.filter((f) => f.type === "talk_to_human")).toEqual([]);
    vi.advanceTimersByTime(1000);
    ws = last();
    ws.open();
    expect(ws.sent).toEqual([{ type: "auth", token: "tok" }]); // not before auth
    ws.receive({ type: "ready", status: "bot" });
    expect(ws.sent.filter((f) => f.type === "talk_to_human")).toHaveLength(1);
    ws.drop(1011); // sent once: a later reconnect does not repeat it
    vi.advanceTimersByTime(2000);
    last().open();
    last().receive({ type: "ready", status: "open" });
    expect(last().sent.filter((f) => f.type === "talk_to_human")).toEqual([]);
  });
});
