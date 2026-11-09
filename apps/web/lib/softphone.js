// The agent's browser softphone: takes transferred calls and places callbacks. The Twilio Voice SDK is
// imported only when the softphone starts, so it never ships in a route's first-load bundle.
// States: off -> registering -> ready <-> ringing (incoming) -> in_call -> ready, or error with reason
// mic_denied | not_available | register_failed, or unavailable (the organisation has no browser calling: a quiet
// state, kept until stop()). The token is fetched before the microphone is asked for, so an organisation without
// voice never sees a permission prompt. While ready, ringing or in a call, a heartbeat tells the server the
// softphone is reachable (it forgets a softphone after 60 s without one).
// A signaling drop (network change, sleep) makes the SDK emit "unregistered" and re-register by itself: the
// state is kept (a live call goes on) and emitted with `reconnecting: true`, the heartbeat pauses, and
// "registered" resumes both. Only a failed register() or a fatal token error ends in error; a fatal token error
// during a call waits for the call to end (the customer is never hung up on for it). A failed token refresh is
// retried TOKEN_RETRY_MS apart before the expiry error decides.

export const HEARTBEAT_MS = 25_000;
export const TOKEN_RETRY_MS = [2_000, 5_000, 10_000];

// Twilio access-token errors that a re-registration cannot fix (invalid, expired, bad signature...).
const FATAL_DEVICE_ERRORS = new Set([20101, 20102, 20103, 20104, 20105, 20106, 20107, 20151, 31202, 31204, 31205]);

export function createSoftphone({ fetchToken, heartbeat, clear, loadSdk = () => import("@twilio/voice-sdk"), onState }) {
  let device = null;
  let call = null;
  let state = "off";
  let muted = false;
  let dialling = false; // callCustomer() is waiting for connect()
  let timer = null;
  let generation = 0;
  let reconnecting = false; // signaling dropped; the SDK is re-registering
  let beaten = false; // a heartbeat was sent since the last clear: the server may think we are reachable
  let clearing = Promise.resolve(); // the last DELETE; a new heartbeat waits for it
  let pendingFailure = null; // a fatal device error during a call: the softphone fails once the call ends

  function emit(next, reason) {
    state = next;
    const out = { state: next };
    if (reason) out.reason = reason;
    if (reconnecting) out.reconnecting = true;
    onState?.(out);
  }

  function sendClear() {
    beaten = false;
    clearing = Promise.resolve()
      .then(() => clear?.())
      .catch(() => {}); // the server forgets it after 60 s anyway
    return clearing;
  }

  function beat() {
    beaten = true;
    Promise.resolve()
      .then(() => heartbeat?.())
      .catch(() => {}); // a missed beat is retried in 25 s
  }

  function startHeartbeat() {
    if (timer) return;
    beat();
    timer = setInterval(beat, HEARTBEAT_MS);
  }

  function stopHeartbeat() {
    clearInterval(timer);
    timer = null;
  }

  function dropCall() {
    try {
      call?.disconnect();
    } catch {
      /* already gone */
    }
    call = null;
    muted = false;
  }

  function teardown() {
    stopHeartbeat();
    dropCall();
    const old = device;
    device = null;
    if (!old) return Promise.resolve();
    return Promise.resolve()
      .then(() => old.unregister?.())
      .catch(() => {})
      .finally(() => {
        try {
          old.destroy();
        } catch {
          /* already gone */
        }
      });
  }

  function fail(reason) {
    generation += 1;
    const wasBeaten = beaten;
    reconnecting = false;
    pendingFailure = null;
    teardown();
    emit("error", reason);
    if (wasBeaten) sendClear();
  }

  // No call any more: back to ready, or the failure deferred while the call lasted.
  function afterCall() {
    if (pendingFailure) fail(pendingFailure);
    else if (device) emit("ready");
  }

  // The call `c` ended (hung up, cancelled, rejected or failed).
  function ended(c) {
    if (call !== c) return;
    call = null;
    muted = false;
    afterCall();
  }

  // Refreshes the device's token; a failure is retried TOKEN_RETRY_MS apart while `created` is still current.
  async function refreshToken(created) {
    for (let retry = 0; ; retry += 1) {
      try {
        const fresh = await fetchToken();
        if (device === created) created.updateToken(fresh.token);
        return;
      } catch {
        if (retry >= TOKEN_RETRY_MS.length || device !== created) return; // the expiry error decides
      }
      await new Promise((resolve) => setTimeout(resolve, TOKEN_RETRY_MS[retry]));
      if (device !== created) return;
    }
  }

  function wire(c) {
    c.on("accept", () => {
      if (call === c && state !== "in_call") emit("in_call");
    });
    for (const ev of ["disconnect", "cancel", "reject"]) c.on(ev, () => ended(c));
    c.on("error", () => {
      if (call !== c) return;
      try {
        c.disconnect(); // it may still be live: never leave an untracked call behind
      } catch {
        /* already gone */
      }
      ended(c);
    });
  }

  async function start() {
    if (state !== "off" && state !== "error") return;
    const attempt = ++generation;
    const stale = () => attempt !== generation;
    pendingFailure = null;
    emit("registering");
    let token;
    try {
      ({ token } = await fetchToken());
    } catch (err) {
      if (stale()) return;
      if (err?.status === 409 && err?.code === "voice_not_available") {
        emit("unavailable"); // nothing to set up here: no prompt, no error line, no retry
        return;
      }
      fail(err?.status === 409 ? "not_available" : "register_failed");
      return;
    }
    if (stale()) return;
    let probe;
    try {
      probe = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      if (!stale()) fail("mic_denied");
      return;
    }
    probe.getTracks().forEach((t) => t.stop());
    if (stale()) return;
    let created = null;
    try {
      const { Device } = await loadSdk();
      if (stale()) return;
      created = new Device(token, { codecPreferences: ["opus", "pcmu"], closeProtection: true });
      device = created;
      try {
        created.audio?.incoming?.(false); // the incoming toast plays its own ring
      } catch {
        /* not supported: the SDK rings too */
      }
      created.on("incoming", (incoming) => {
        if (device !== created) return;
        if (call || dialling || state !== "ready" || reconnecting) {
          incoming.reject(); // busy: the transfer moves on to the next agent
          return;
        }
        call = incoming;
        wire(incoming);
        emit("ringing");
      });
      created.on("tokenWillExpire", () => refreshToken(created));
      // A listener is needed anyway (an unhandled emitter "error" throws). Call errors arrive on the call.
      created.on("error", (error) => {
        if (device !== created || state === "registering" || !FATAL_DEVICE_ERRORS.has(error?.code)) return;
        if (state === "in_call" && call) {
          pendingFailure = "register_failed"; // the call goes on; the softphone fails when it ends
          stopHeartbeat(); // no new call can reach this device
          return;
        }
        fail("register_failed");
      });
      created.on("unregistered", () => {
        if (device !== created || state === "registering" || reconnecting) return;
        reconnecting = true;
        stopHeartbeat();
        emit(state);
      });
      created.on("registered", () => {
        if (device !== created || !reconnecting) return;
        reconnecting = false;
        emit(state);
        if (!pendingFailure) startHeartbeat();
      });
      await created.register();
      if (stale()) return;
      await clearing; // a DELETE from an earlier stop() must not land after our first heartbeat
      if (stale()) return;
      emit("ready");
      startHeartbeat();
    } catch (err) {
      if (stale()) {
        if (created && device !== created) {
          try {
            created.destroy();
          } catch {
            /* already gone */
          }
        }
        return;
      }
      fail("register_failed");
    }
  }

  async function stop() {
    const wasOn = state !== "off" && state !== "error" && state !== "unavailable";
    generation += 1;
    reconnecting = false;
    pendingFailure = null;
    const done = teardown();
    emit("off");
    const cleared = wasOn || beaten ? sendClear() : clearing;
    await Promise.all([done, cleared]);
  }

  function accept() {
    if (state !== "ringing" || !call) return;
    call.accept();
  }

  function decline() {
    if (state !== "ringing" || !call) return;
    const c = call;
    call = null;
    try {
      c.reject();
    } catch {
      /* already gone */
    }
    emit("ready");
  }

  // Calls a customer back with a callback ticket from the API. Resolves true once the call is placed.
  async function callCustomer(ticket) {
    if (state !== "ready" || reconnecting || !device || dialling) return false;
    const current = device;
    dialling = true;
    try {
      const placed = await current.connect({ params: { callback_ticket: ticket } });
      if (device !== current) {
        try {
          placed?.disconnect();
        } catch {
          /* already gone */
        }
        return false;
      }
      call = placed;
      wire(placed);
      emit("in_call");
      return true;
    } finally {
      dialling = false;
    }
  }

  function hangUp() {
    if (!call) return;
    dropCall();
    afterCall();
  }

  function toggleMute() {
    if (!call || state !== "in_call") return muted;
    muted = !muted;
    call.mute(muted);
    return muted;
  }

  return { start, stop, accept, decline, callCustomer, hangUp, toggleMute };
}

// "Take calls" is remembered per organisation and member in this browser. Without a remembered choice it
// follows the member's call preference (on for "browser"). Storage may be blocked: then the default wins.
export const takeCallsKey = (orgId, membershipId) => `softphone:take-calls:${orgId}:${membershipId}`;

function browserStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function loadTakeCalls(key, preference, storage = browserStorage()) {
  try {
    const saved = storage?.getItem(key);
    if (saved === "on") return true;
    if (saved === "off") return false;
  } catch {
    /* blocked storage: use the default */
  }
  return preference !== "phone";
}

export function saveTakeCalls(key, on, storage = browserStorage()) {
  try {
    storage?.setItem(key, on ? "on" : "off");
  } catch {
    /* blocked storage: the choice lasts for this page only */
  }
}
