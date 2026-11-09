// Browser call to the voice bot. The Twilio Voice SDK is imported only when a call starts, so it
// never ships in the first-load embed bundle. States: idle -> requesting_mic -> connecting ->
// in_call -> ended, or failed with reason mic_denied | not_available | rate_limited | connect_failed.

export function createCallController({ fetchToken, loadSdk = () => import("@twilio/voice-sdk"), onState }) {
  let device = null;
  let call = null;
  let muted = false;
  let busy = false;
  let generation = 0;

  function emit(state, reason) {
    onState?.(reason ? { state, reason } : { state });
  }

  function teardown() {
    try {
      call?.disconnect();
    } catch {
      /* already gone */
    }
    try {
      device?.destroy();
    } catch {
      /* already gone */
    }
    call = null;
    device = null;
    muted = false;
  }

  function fail(reason) {
    generation += 1;
    teardown();
    busy = false;
    emit("failed", reason);
  }

  async function start() {
    if (busy) return;
    busy = true;
    const attempt = ++generation;
    const stale = () => attempt !== generation;
    emit("requesting_mic");
    let probe;
    try {
      probe = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      if (!stale()) fail("mic_denied");
      return;
    }
    probe.getTracks().forEach((t) => t.stop());
    if (stale()) return;
    emit("connecting");
    let created = null;
    try {
      const { token, ticket } = await fetchToken();
      if (stale()) return;
      const { Device } = await loadSdk();
      if (stale()) return;
      created = new Device(token, { codecPreferences: ["opus", "pcmu"] });
      device = created;
      created.on?.("error", () => {
        if (device === created) fail("connect_failed");
      });
      const active = await created.connect({ params: { ticket } });
      if (stale()) {
        // hung up while connecting: do not leave a live call behind
        try {
          active?.disconnect();
        } catch {
          /* already gone */
        }
        try {
          created.destroy();
        } catch {
          /* already gone */
        }
        return;
      }
      call = active;
      active.on("accept", () => {
        if (call === active) emit("in_call");
      });
      active.on("disconnect", () => {
        if (call !== active) return;
        teardown();
        busy = false;
        emit("ended");
      });
      active.on("error", () => {
        if (call === active) fail("connect_failed");
      });
    } catch (err) {
      if (stale()) {
        try {
          created?.destroy();
        } catch {
          /* already gone */
        }
        return;
      }
      fail(err?.status === 409 ? "not_available" : err?.status === 429 ? "rate_limited" : "connect_failed");
    }
  }

  function hangUp() {
    if (!busy && !device) return;
    generation += 1; // invalidates a start() still waiting on mic, token, SDK or connect
    teardown();
    busy = false;
    emit("ended");
  }

  function toggleMute() {
    if (!call) return muted;
    muted = !muted;
    call.mute(muted);
    return muted;
  }

  return { start, hangUp, toggleMute };
}

// Every browser call creates its own conversation, so when a call ends the newest conversation
// (the API lists most recent activity first) is the call's, whether or not a chat was open.
export async function loadCallConversation(client) {
  const list = await client.listConversations();
  const items = Array.isArray(list) ? list : list?.items || [];
  const newest = items[0];
  if (!newest) return null;
  const history = await client.messages(newest.id);
  return { conversation: newest, history };
}
