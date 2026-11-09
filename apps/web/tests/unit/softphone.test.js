import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createSoftphone, HEARTBEAT_MS, loadTakeCalls, saveTakeCalls, takeCallsKey } from "@/lib/softphone";
import { callTitle, transferLabel, endReasonLabel } from "@/lib/inbox";

function emitter(target) {
  const handlers = {};
  target.on = vi.fn((ev, fn) => {
    handlers[ev] = fn;
    return target;
  });
  target.fire = (ev, ...args) => handlers[ev]?.(...args);
  return target;
}

function makeCall() {
  const call = emitter({ accept: vi.fn(), reject: vi.fn(), disconnect: vi.fn(), mute: vi.fn() });
  call.accept.mockImplementation(() => call.fire("accept"));
  return call;
}

function setup({ mic = "ok", tokenError = null } = {}) {
  const track = { stop: vi.fn() };
  const getUserMedia = vi.fn(async () => {
    if (mic === "denied") throw new DOMException("no", "NotAllowedError");
    return { getTracks: () => [track] };
  });
  Object.defineProperty(globalThis.navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
  const outgoing = makeCall();
  const device = emitter({
    register: vi.fn(async () => {}),
    unregister: vi.fn(async () => {}),
    destroy: vi.fn(),
    updateToken: vi.fn(),
    connect: vi.fn(async () => outgoing),
    audio: { incoming: vi.fn() },
  });
  const Device = vi.fn(function () {
    return device;
  });
  let n = 0;
  const fetchToken = vi.fn(async () => {
    if (tokenError) throw tokenError;
    n += 1;
    return { token: `jwt-${n}`, identity: "agent-m1", expires_in: 3600 };
  });
  const heartbeat = vi.fn(async () => {});
  const clear = vi.fn(async () => {});
  const loadSdk = vi.fn(async () => ({ Device }));
  const states = [];
  const onState = vi.fn((s) => states.push(s));
  const phone = createSoftphone({ fetchToken, heartbeat, clear, loadSdk, onState });
  return { phone, device, Device, outgoing, fetchToken, heartbeat, clear, loadSdk, states, track, getUserMedia };
}

const last = (states) => states.at(-1).state;

describe("createSoftphone", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("sdk loaded lazily", async () => {
    const { phone, loadSdk, Device } = setup();
    expect(loadSdk).not.toHaveBeenCalled();
    expect(Device).not.toHaveBeenCalled();
    await phone.start();
    expect(loadSdk).toHaveBeenCalledTimes(1);
    expect(Device).toHaveBeenCalledTimes(1);
  });

  it("registers and heartbeats", async () => {
    const t = setup();
    await t.phone.start();
    expect(t.track.stop).toHaveBeenCalled();
    expect(t.Device).toHaveBeenCalledWith("jwt-1", { codecPreferences: ["opus", "pcmu"], closeProtection: true });
    expect(t.device.register).toHaveBeenCalled();
    expect(t.states.map((s) => s.state)).toEqual(["registering", "ready"]);
    expect(t.heartbeat).toHaveBeenCalledTimes(1);
    expect(HEARTBEAT_MS).toBe(25000);
    await vi.advanceTimersByTimeAsync(25000);
    expect(t.heartbeat).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(50000);
    expect(t.heartbeat).toHaveBeenCalledTimes(4);
  });

  it("mic denied fails with mic_denied and loads no device", async () => {
    const t = setup({ mic: "denied" });
    await t.phone.start();
    expect(t.states.at(-1)).toEqual({ state: "error", reason: "mic_denied" });
    expect(t.Device).not.toHaveBeenCalled();
  });

  it("asks for the token before the microphone", async () => {
    const t = setup();
    const order = [];
    t.fetchToken.mockImplementation(async () => {
      order.push("token");
      return { token: "jwt" };
    });
    t.getUserMedia.mockImplementation(async () => {
      order.push("mic");
      return { getTracks: () => [t.track] };
    });
    await t.phone.start();
    expect(order).toEqual(["token", "mic"]);
  });

  it("a 409 voice_not_available is a quiet unavailable state: no microphone prompt, no retry", async () => {
    const t = setup({ tokenError: { status: 409, code: "voice_not_available" } });
    await t.phone.start();
    expect(t.states.at(-1)).toEqual({ state: "unavailable" });
    expect(t.getUserMedia).not.toHaveBeenCalled();
    expect(t.loadSdk).not.toHaveBeenCalled();
    await t.phone.start(); // not an error state: start() does not retry it
    await vi.advanceTimersByTimeAsync(120000);
    expect(t.fetchToken).toHaveBeenCalledTimes(1);
    expect(t.heartbeat).not.toHaveBeenCalled();
    expect(t.clear).not.toHaveBeenCalled();
  });

  it("another 409 from the token fails with not_available; other errors register_failed", async () => {
    const a = setup({ tokenError: { status: 409 } });
    await a.phone.start();
    expect(a.states.at(-1)).toEqual({ state: "error", reason: "not_available" });
    const b = setup();
    b.device.register.mockRejectedValue(new Error("31204"));
    await b.phone.start();
    expect(b.states.at(-1)).toEqual({ state: "error", reason: "register_failed" });
    await vi.advanceTimersByTimeAsync(0);
    expect(b.device.destroy).toHaveBeenCalled();
    expect(b.heartbeat).not.toHaveBeenCalled();
  });

  it("incoming call rings then accept goes in_call", async () => {
    const t = setup();
    await t.phone.start();
    const call = makeCall();
    t.device.fire("incoming", call);
    expect(last(t.states)).toBe("ringing");
    t.phone.accept();
    expect(call.accept).toHaveBeenCalled();
    expect(last(t.states)).toBe("in_call");
    t.phone.toggleMute();
    expect(call.mute).toHaveBeenLastCalledWith(true);
    call.fire("disconnect");
    expect(last(t.states)).toBe("ready");
    await vi.advanceTimersByTimeAsync(25000);
    expect(t.heartbeat).toHaveBeenCalledTimes(2);
  });

  it("an incoming call cancelled by the caller goes back to ready", async () => {
    const t = setup();
    await t.phone.start();
    const call = makeCall();
    t.device.fire("incoming", call);
    call.fire("cancel");
    expect(last(t.states)).toBe("ready");
  });

  it("decline rejects", async () => {
    const t = setup();
    await t.phone.start();
    const call = makeCall();
    t.device.fire("incoming", call);
    t.phone.decline();
    expect(call.reject).toHaveBeenCalled();
    expect(last(t.states)).toBe("ready");
  });

  it("a second incoming call while busy is rejected", async () => {
    const t = setup();
    await t.phone.start();
    const first = makeCall();
    t.device.fire("incoming", first);
    const second = makeCall();
    t.device.fire("incoming", second);
    expect(second.reject).toHaveBeenCalled();
    expect(first.reject).not.toHaveBeenCalled();
  });

  it("callCustomer passes the ticket", async () => {
    const t = setup();
    await t.phone.start();
    await t.phone.callCustomer("cb-ticket");
    expect(t.device.connect).toHaveBeenCalledWith({ params: { callback_ticket: "cb-ticket" } });
    expect(last(t.states)).toBe("in_call");
    t.phone.hangUp();
    expect(t.outgoing.disconnect).toHaveBeenCalled();
    expect(last(t.states)).toBe("ready");
  });

  it("stop unregisters and clears softphone", async () => {
    const t = setup();
    await t.phone.start();
    await t.phone.stop();
    expect(t.device.unregister).toHaveBeenCalled();
    expect(t.device.destroy).toHaveBeenCalled();
    expect(t.clear).toHaveBeenCalled();
    expect(last(t.states)).toBe("off");
    await vi.advanceTimersByTimeAsync(60000);
    expect(t.heartbeat).toHaveBeenCalledTimes(1);
  });

  it("stop during registration leaves no device behind", async () => {
    const t = setup();
    let release;
    t.fetchToken.mockImplementation(() => new Promise((r) => (release = r)));
    const started = t.phone.start();
    await vi.advanceTimersByTimeAsync(0);
    await t.phone.stop();
    release({ token: "late" });
    await started;
    expect(t.Device).not.toHaveBeenCalled();
    expect(last(t.states)).toBe("off");
  });

  it("token refresh on tokenWillExpire", async () => {
    const t = setup();
    await t.phone.start();
    t.device.fire("tokenWillExpire");
    await vi.advanceTimersByTimeAsync(0);
    expect(t.fetchToken).toHaveBeenCalledTimes(2);
    expect(t.device.updateToken).toHaveBeenCalledWith("jwt-2");
  });

  it("a failed token refresh is retried at 2 s, 5 s and 10 s", async () => {
    const t = setup();
    await t.phone.start();
    t.fetchToken.mockRejectedValueOnce(new Error("blip")).mockRejectedValueOnce(new Error("blip"));
    t.device.fire("tokenWillExpire");
    await vi.advanceTimersByTimeAsync(0);
    expect(t.fetchToken).toHaveBeenCalledTimes(2); // start + the first refresh, which failed
    await vi.advanceTimersByTimeAsync(1999);
    expect(t.fetchToken).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(t.fetchToken).toHaveBeenCalledTimes(3); // failed again
    expect(t.device.updateToken).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5000);
    expect(t.fetchToken).toHaveBeenCalledTimes(4);
    expect(t.device.updateToken).toHaveBeenCalledWith("jwt-2");
    await vi.advanceTimersByTimeAsync(60000);
    expect(t.fetchToken).toHaveBeenCalledTimes(4); // done once it worked
  });

  it("token refresh gives up after three retries", async () => {
    const t = setup();
    await t.phone.start();
    t.fetchToken.mockRejectedValue(new Error("down"));
    t.device.fire("tokenWillExpire");
    await vi.advanceTimersByTimeAsync(2000 + 5000 + 10000);
    expect(t.fetchToken).toHaveBeenCalledTimes(5); // start, the refresh and its three retries
    await vi.advanceTimersByTimeAsync(60000);
    expect(t.fetchToken).toHaveBeenCalledTimes(5);
    expect(last(t.states)).toBe("ready"); // the expiry error, if it comes, decides
  });

  it("a fatal token error during a call waits for the call to end", async () => {
    const t = setup();
    await t.phone.start();
    const call = makeCall();
    t.device.fire("incoming", call);
    t.phone.accept();
    t.device.fire("error", { code: 20104 });
    expect(last(t.states)).toBe("in_call");
    expect(call.disconnect).not.toHaveBeenCalled();
    expect(t.device.destroy).not.toHaveBeenCalled();
    t.phone.toggleMute(); // the call still works
    expect(call.mute).toHaveBeenLastCalledWith(true);
    call.fire("disconnect");
    expect(t.states.at(-1)).toEqual({ state: "error", reason: "register_failed" });
    await vi.advanceTimersByTimeAsync(0);
    expect(t.device.destroy).toHaveBeenCalled();
    expect(t.clear).toHaveBeenCalled();
  });

  it("a deferred fatal error also fails when the agent hangs up", async () => {
    const t = setup();
    await t.phone.start();
    await t.phone.callCustomer("tk");
    t.device.fire("error", { code: 20101 });
    expect(last(t.states)).toBe("in_call");
    t.phone.hangUp();
    expect(t.states.at(-1)).toEqual({ state: "error", reason: "register_failed" });
  });

  it("a signaling blip during a live call keeps the call", async () => {
    const t = setup();
    await t.phone.start();
    const call = makeCall();
    t.device.fire("incoming", call);
    t.phone.accept();
    t.device.fire("unregistered");
    expect(t.states.at(-1)).toEqual({ state: "in_call", reconnecting: true });
    expect(call.disconnect).not.toHaveBeenCalled();
    expect(t.device.destroy).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(25000);
    expect(t.heartbeat).toHaveBeenCalledTimes(1); // no beat while unregistered
    t.device.fire("registered");
    expect(t.states.at(-1)).toEqual({ state: "in_call" });
    await vi.advanceTimersByTimeAsync(0);
    expect(t.heartbeat).toHaveBeenCalledTimes(2);
    t.phone.toggleMute();
    expect(call.mute).toHaveBeenLastCalledWith(true);
  });

  it("a signaling blip while idle comes back to ready with the heartbeat restarted", async () => {
    const t = setup();
    await t.phone.start();
    t.device.fire("unregistered");
    expect(t.states.at(-1)).toEqual({ state: "ready", reconnecting: true });
    await vi.advanceTimersByTimeAsync(50000);
    expect(t.heartbeat).toHaveBeenCalledTimes(1);
    expect(t.clear).not.toHaveBeenCalled();
    t.device.fire("registered");
    expect(t.states.at(-1)).toEqual({ state: "ready" });
    await vi.advanceTimersByTimeAsync(0);
    expect(t.heartbeat).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(25000);
    expect(t.heartbeat).toHaveBeenCalledTimes(3);
  });

  it("a fatal token error fails and clears the softphone", async () => {
    const t = setup();
    await t.phone.start();
    t.device.fire("error", { code: 20101 });
    expect(t.states.at(-1)).toEqual({ state: "error", reason: "register_failed" });
    await vi.advanceTimersByTimeAsync(0);
    expect(t.device.destroy).toHaveBeenCalled();
    expect(t.clear).toHaveBeenCalled();
  });

  it("a non-fatal device error changes nothing", async () => {
    const t = setup();
    await t.phone.start();
    t.device.fire("error", { code: 31005 });
    expect(last(t.states)).toBe("ready");
  });

  it("a call error disconnects the call before forgetting it", async () => {
    const t = setup();
    expect(await t.phone.callCustomer("tk")).toBe(false); // not started: ignored
    await t.phone.start();
    await t.phone.callCustomer("tk");
    t.outgoing.fire("error", new Error("31000"));
    expect(t.outgoing.disconnect).toHaveBeenCalled();
    expect(last(t.states)).toBe("ready");
  });

  it("start after stop waits for the DELETE before the first heartbeat", async () => {
    const t = setup();
    await t.phone.start();
    let release;
    t.clear.mockImplementation(() => new Promise((r) => (release = r)));
    const order = [];
    t.heartbeat.mockImplementation(async () => order.push("beat"));
    const stopped = t.phone.stop();
    const started = t.phone.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(order).toEqual([]);
    order.push("clear");
    release();
    await stopped;
    await started;
    await vi.advanceTimersByTimeAsync(0);
    expect(order).toEqual(["clear", "beat"]);
  });
});

describe("inbox call labels", () => {
  it("transferLabel covers every status", () => {
    expect(transferLabel({ status: "connected", connected_agent_name: "Ann" })).toBe("Transferred to Ann");
    expect(transferLabel({ status: "connected", connected_agent_name: null })).toBe("Transferred to an agent");
    expect(transferLabel({ status: "no_answer" })).toBe("No one answered");
    expect(transferLabel({ status: "abandoned" })).toBe("Caller left while waiting");
    expect(transferLabel({ status: "ringing" })).toBe("Ringing agents");
    expect(transferLabel({ status: "failed" })).toBe("Transfer failed");
    expect(transferLabel(null)).toBe("");
  });

  it("callTitle names direction", () => {
    expect(callTitle({ direction: "inbound" })).toBe("Incoming call");
    expect(callTitle({ direction: "outbound", agent_name: "Bo" })).toBe("Callback by Bo");
    expect(callTitle({ direction: "outbound", agent_name: null })).toBe("Callback");
    expect(callTitle({ source: "phone" })).toBe("Incoming call");
  });

  it("the new end reasons have labels", () => {
    expect(endReasonLabel("transferred")).toBe("Transferred");
    expect(endReasonLabel("transfer_no_answer")).toBe("No one answered the transfer");
    expect(endReasonLabel("callback")).toBe("Callback");
  });
});

describe("take calls preference", () => {
  const key = takeCallsKey("o1", "m1");

  it("defaults from the call preference and remembers the toggle", () => {
    const store = new Map();
    const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
    expect(loadTakeCalls(key, "browser", storage)).toBe(true);
    expect(loadTakeCalls(key, "phone", storage)).toBe(false);
    saveTakeCalls(key, false, storage);
    expect(loadTakeCalls(key, "browser", storage)).toBe(false);
    saveTakeCalls(key, true, storage);
    expect(loadTakeCalls(key, "phone", storage)).toBe(true);
  });

  it("storage that throws falls back to the preference", () => {
    const storage = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(loadTakeCalls(key, "browser", storage)).toBe(true);
    expect(() => saveTakeCalls(key, false, storage)).not.toThrow();
  });
});
