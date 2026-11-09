import { describe, it, expect, vi } from "vitest";
import { createCallController, loadCallConversation } from "@/lib/voice-call";

function setup({ mic = "ok", tokenError = null } = {}) {
  const track = { stop: vi.fn() };
  const stream = { getTracks: () => [track] };
  const getUserMedia = vi.fn(async () => {
    if (mic === "denied") throw new DOMException("no", "NotAllowedError");
    return stream;
  });
  Object.defineProperty(globalThis.navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
  const call = { mute: vi.fn(), disconnect: vi.fn(), on: vi.fn() };
  const handlers = {};
  call.on = vi.fn((ev, fn) => {
    handlers[ev] = fn;
  });
  const deviceHandlers = {};
  const device = {
    connect: vi.fn(async () => {
      setTimeout(() => handlers.accept?.(), 0);
      return call;
    }),
    destroy: vi.fn(),
    on: vi.fn((ev, fn) => {
      deviceHandlers[ev] = fn;
    }),
  };
  const Device = vi.fn(function () {
    return device;
  });
  const fetchToken = vi.fn(async () => {
    if (tokenError) throw tokenError;
    return { token: "jwt", ticket: "tk", expires_in: 600 };
  });
  const loadSdk = vi.fn(async () => ({ Device }));
  const states = [];
  const onState = vi.fn((s) => states.push(s));
  const ctl = createCallController({ fetchToken, loadSdk, onState });
  return { handlers, deviceHandlers, ctl, call, device, Device, fetchToken, loadSdk, getUserMedia, track, states };
}

describe("createCallController", () => {
  it("sdk is loaded lazily", () => {
    const { loadSdk } = setup();
    expect(loadSdk).not.toHaveBeenCalled();
  });

  it("start goes through states to in_call", async () => {
    const { ctl, call, Device, device, track, states } = setup();
    await ctl.start();
    await new Promise((r) => setTimeout(r, 0));
    expect(states.map((s) => s.state)).toEqual(["requesting_mic", "connecting", "in_call"]);
    expect(track.stop).toHaveBeenCalled();
    expect(Device).toHaveBeenCalledWith("jwt", { codecPreferences: ["opus", "pcmu"] });
    expect(device.connect).toHaveBeenCalledWith({ params: { ticket: "tk" } });
    expect(call.on).toHaveBeenCalledWith("disconnect", expect.any(Function));
  });

  it("mic denied fails with mic_denied without fetching a token", async () => {
    const { ctl, fetchToken, loadSdk, states } = setup({ mic: "denied" });
    await ctl.start();
    expect(states.at(-1)).toEqual({ state: "failed", reason: "mic_denied" });
    expect(fetchToken).not.toHaveBeenCalled();
    expect(loadSdk).not.toHaveBeenCalled();
  });

  it("409 from token fails with not_available", async () => {
    const { ctl, states } = setup({ tokenError: { status: 409 } });
    await ctl.start();
    expect(states.at(-1)).toEqual({ state: "failed", reason: "not_available" });
  });

  it("429 from token fails with rate_limited", async () => {
    const { ctl, states } = setup({ tokenError: { status: 429 } });
    await ctl.start();
    expect(states.at(-1)).toEqual({ state: "failed", reason: "rate_limited" });
  });

  it("other errors fail with connect_failed", async () => {
    const { ctl, states } = setup({ tokenError: new Error("boom") });
    await ctl.start();
    expect(states.at(-1)).toEqual({ state: "failed", reason: "connect_failed" });
  });

  it("hangUp disconnects and destroys", async () => {
    const { ctl, call, device, states } = setup();
    await ctl.start();
    ctl.hangUp();
    expect(call.disconnect).toHaveBeenCalled();
    expect(device.destroy).toHaveBeenCalled();
    expect(states.at(-1).state).toBe("ended");
  });

  it("remote disconnect ends the call and destroys the device", async () => {
    const { ctl, call, device, states } = setup();
    await ctl.start();
    await new Promise((r) => setTimeout(r, 0));
    call.on.mock.calls.find((c) => c[0] === "disconnect")[1]();
    expect(device.destroy).toHaveBeenCalled();
    expect(states.at(-1).state).toBe("ended");
  });

  it("toggleMute calls call.mute", async () => {
    const { ctl, call } = setup();
    await ctl.start();
    expect(ctl.toggleMute()).toBe(true);
    expect(call.mute).toHaveBeenLastCalledWith(true);
    expect(ctl.toggleMute()).toBe(false);
    expect(call.mute).toHaveBeenLastCalledWith(false);
  });

  it("in_call is emitted on the call accept event, not when connect resolves", async () => {
    const { ctl, device, handlers, states } = setup();
    device.connect.mockImplementation(async () => call_());
    function call_() {
      return { mute: vi.fn(), disconnect: vi.fn(), on: vi.fn((ev, fn) => (handlers[ev] = fn)) };
    }
    await ctl.start();
    expect(states.map((s) => s.state)).toEqual(["requesting_mic", "connecting"]);
    handlers.accept();
    expect(states.at(-1).state).toBe("in_call");
  });

  it("device error fails the call with connect_failed", async () => {
    const { ctl, deviceHandlers, device, states } = setup();
    await ctl.start();
    deviceHandlers.error(new Error("31205"));
    expect(device.destroy).toHaveBeenCalled();
    expect(states.at(-1)).toEqual({ state: "failed", reason: "connect_failed" });
  });

  it("hangUp during requesting_mic leaves no device and stops the stream", async () => {
    const t = setup();
    let release;
    const track = { stop: vi.fn() };
    navigator.mediaDevices.getUserMedia.mockImplementation(() => new Promise((r) => (release = r)));
    const started = t.ctl.start();
    t.ctl.hangUp();
    const n = t.states.length;
    release({ getTracks: () => [track] });
    await started;
    expect(track.stop).toHaveBeenCalled();
    expect(t.fetchToken).not.toHaveBeenCalled();
    expect(t.Device).not.toHaveBeenCalled();
    expect(t.states.length).toBe(n);
  });

  it("hangUp during the token fetch creates no device", async () => {
    const t = setup();
    let release;
    t.fetchToken.mockImplementation(() => new Promise((r) => (release = r)));
    const started = t.ctl.start();
    await new Promise((r) => setTimeout(r, 0));
    t.ctl.hangUp();
    const n = t.states.length;
    release({ token: "jwt", ticket: "tk" });
    await started;
    expect(t.Device).not.toHaveBeenCalled();
    expect(t.states.length).toBe(n);
  });

  it("hangUp during connect disconnects the call and destroys the device", async () => {
    const t = setup();
    let release;
    t.device.connect.mockImplementation(() => new Promise((r) => (release = r)));
    const started = t.ctl.start();
    await new Promise((r) => setTimeout(r, 0));
    t.ctl.hangUp();
    const n = t.states.length;
    release(t.call);
    await started;
    expect(t.call.disconnect).toHaveBeenCalled();
    expect(t.device.destroy).toHaveBeenCalled();
    expect(t.states.length).toBe(n);
    expect(t.states.some((s) => s.state === "in_call")).toBe(false);
  });
});

describe("loadCallConversation", () => {
  it("switches to the newest conversation even when a chat is already open", async () => {
    const client = {
      listConversations: vi.fn(async () => [
        { id: "call-conv", status: "open", agent: null },
        { id: "open-chat", status: "bot" },
      ]),
      messages: vi.fn(async () => [{ id: "m1" }]),
    };
    const out = await loadCallConversation(client);
    expect(out.conversation.id).toBe("call-conv");
    expect(client.messages).toHaveBeenCalledWith("call-conv");
    expect(out.history).toEqual([{ id: "m1" }]);
  });

  it("returns null when there are no conversations", async () => {
    expect(await loadCallConversation({ listConversations: async () => [], messages: vi.fn() })).toBeNull();
  });
});
