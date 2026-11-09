"use client";

// The agent's browser softphone, for members who may answer queries: takes transferred calls (an incoming
// toast) and places callbacks (the in-call bar). The Twilio SDK loads only when "Take calls" is on, through
// lib/softphone's dynamic import, so no route's first-load bundle carries it.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { createSoftphone, loadTakeCalls, saveTakeCalls, takeCallsKey } from "@/lib/softphone";
import { beatSoftphone, clearSoftphone, getAgentToken, getCallbackTicket } from "@/lib/voice";
import { useInboxRealtime } from "@/components/inbox/realtime-provider";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { IncomingCall } from "@/components/voice/incoming-call";
import { InCallBar } from "@/components/voice/in-call-bar";
import { MyCallSettingsDialog } from "@/components/voice/my-call-settings-dialog";

const SoftphoneContext = createContext(null);

// null outside the provider (members who cannot answer queries).
export function useSoftphone() {
  return useContext(SoftphoneContext);
}

const STATUS_LINES = {
  registering: "Connecting...",
  ready: "Ready for calls",
  ringing: "Incoming call",
  in_call: "On a call",
};
const ERROR_LINES = {
  mic_denied: "Allow the microphone in your browser to take calls.",
  not_available: "Browser calls are not set up for this organisation.",
  register_failed: "Could not connect the softphone. Turn it off and on to retry.",
};

export function SoftphoneProvider({ orgId, children }) {
  const { api } = useAuth();
  const realtime = useInboxRealtime();
  const membershipId = realtime?.membershipId ?? null;
  const callOffer = realtime?.callOffer ?? null;
  const [me, setMe] = useState(null); // own member row: phone_number, call_preference
  const [enabled, setEnabled] = useState(null); // null until the member row is known
  const [status, setStatus] = useState({ state: "off" });
  const [muted, setMuted] = useState(false);
  const [active, setActive] = useState(null); // {conversationId, startedAt} of the call in progress
  const [settingsOpen, setSettingsOpen] = useState(false);
  const phone = useRef(null);
  const offerRef = useRef(null);
  const nextConversation = useRef(null); // the conversation of the callback being placed
  const offeredConversation = useRef(null); // the newest call_offer's conversation, kept after call_offer_ended
  const stopping = useRef(null); // the previous softphone's stop(): its DELETE goes before our first beat
  useEffect(() => {
    offerRef.current = callOffer;
    // "accepted" may clear the offer before the SDK's accept event: keep the conversation for the in-call bar
    if (callOffer) offeredConversation.current = callOffer.conversation_id;
  }, [callOffer]);

  useEffect(() => {
    if (!membershipId) return undefined;
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/members`)
      .then((members) => {
        const row = members.find((m) => m.membership_id === membershipId);
        if (cancelled || !row) return;
        setMe(row);
        setEnabled(loadTakeCalls(takeCallsKey(orgId, membershipId), row.call_preference));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [api, orgId, membershipId]);

  useEffect(() => {
    if (!enabled) return undefined;
    let previous = "off";
    const onState = (s) => {
      setStatus(s);
      const wasOnCall = previous === "ringing" || previous === "in_call";
      previous = s.state;
      if (s.state === "in_call") {
        // a callback names its conversation; a transferred call's comes from the matching call_offer
        const conversationId =
          nextConversation.current ?? offerRef.current?.conversation_id ?? offeredConversation.current ?? null;
        setActive((cur) => cur ?? { conversationId, startedAt: Date.now() });
      } else if (s.state !== "ringing") {
        if (wasOnCall) {
          nextConversation.current = null;
          offeredConversation.current = null;
        }
        setActive(null);
        setMuted(false);
      }
    };
    const created = createSoftphone({
      fetchToken: () => getAgentToken(api, orgId),
      heartbeat: () => beatSoftphone(api, orgId),
      clear: () => clearSoftphone(api, orgId),
      onState,
    });
    phone.current = created;
    let cancelled = false;
    Promise.resolve(stopping.current).then(() => {
      if (!cancelled) created.start();
    });
    return () => {
      cancelled = true;
      if (phone.current === created) phone.current = null;
      stopping.current = created.stop();
    };
  }, [api, orgId, enabled]);

  const busy = status.state === "ringing" || status.state === "in_call";
  const toggleTakeCalls = useCallback(
    (on) => {
      if (!on && busy && !window.confirm("Turn off Take calls? This hangs up the current call.")) return;
      if (membershipId) saveTakeCalls(takeCallsKey(orgId, membershipId), on);
      setEnabled(on);
    },
    [orgId, membershipId, busy],
  );

  // Calls the customer of `conversationId` back from this browser. Throws the API's error (for
  // voiceErrorMessage); resolves false when the softphone was not ready.
  const callCustomer = useCallback(
    async (conversationId) => {
      const { ticket } = await getCallbackTicket(api, orgId, conversationId);
      if (!phone.current) return false;
      nextConversation.current = conversationId;
      let placed = false;
      try {
        placed = await phone.current.callCustomer(ticket);
        return placed;
      } finally {
        if (!placed) nextConversation.current = null;
      }
    },
    [api, orgId],
  );

  const accept = useCallback(() => phone.current?.accept(), []);
  const decline = useCallback(() => phone.current?.decline(), []);
  const hangUp = useCallback(() => phone.current?.hangUp(), []);
  const toggleMute = useCallback(() => setMuted(Boolean(phone.current?.toggleMute())), []);

  const value = useMemo(
    () => ({
      state: status.state,
      reason: status.reason ?? null,
      reconnecting: Boolean(status.reconnecting),
      enabled: Boolean(enabled),
      known: enabled !== null,
      me,
      membershipId,
      setMe,
      toggleTakeCalls,
      openSettings: () => setSettingsOpen(true),
      callCustomer,
    }),
    [status, enabled, me, membershipId, toggleTakeCalls, callCustomer],
  );

  const conversationHref = (id) => (id ? `/orgs/${orgId}/inbox/${id}` : null);

  return (
    <SoftphoneContext.Provider value={value}>
      {children}
      {status.state === "ringing" && (
        <IncomingCall href={conversationHref(callOffer?.conversation_id)} onAccept={accept} onDecline={decline} />
      )}
      {status.state === "in_call" && active && (
        <InCallBar
          href={conversationHref(active.conversationId)}
          startedAt={active.startedAt}
          muted={muted}
          onToggleMute={toggleMute}
          onHangUp={hangUp}
        />
      )}
      {settingsOpen && me && membershipId && (
        <MyCallSettingsDialog
          open={settingsOpen}
          onOpenChange={setSettingsOpen}
          orgId={orgId}
          membershipId={membershipId}
          member={me}
          onSaved={setMe}
        />
      )}
    </SoftphoneContext.Provider>
  );
}

// The sidebar control: "Take calls" on/off, the softphone's status, and "My call settings". Where the
// organisation has no browser calling ("unavailable") only "My call settings" shows: no switch, no error line.
export function SoftphoneControl() {
  const softphone = useSoftphone();
  if (!softphone?.known) return null;
  const { enabled, state, reason, toggleTakeCalls, openSettings } = softphone;
  const settingsLink = (
    <Button type="button" variant="link" size="sm" className="h-auto self-start p-0 text-xs" onClick={openSettings}>
      My call settings
    </Button>
  );
  if (enabled && state === "unavailable") return <div className="flex flex-col gap-1">{settingsLink}</div>;
  const line = !enabled
    ? "Calls are not rung in this browser."
    : state === "error"
      ? ERROR_LINES[reason]
      : softphone.reconnecting
        ? "Reconnecting..."
        : STATUS_LINES[state];
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor="take-calls" className="text-xs text-muted-foreground">
          Take calls
        </Label>
        <Switch id="take-calls" size="sm" checked={enabled} onCheckedChange={toggleTakeCalls} />
      </div>
      {line && (
        <p className={`text-xs ${state === "error" && enabled ? "text-destructive" : "text-muted-foreground"}`}>{line}</p>
      )}
      {settingsLink}
    </div>
  );
}
