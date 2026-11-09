"use client";

// One inbox socket per open organisation, shared by the shell (nav counts, bell, status switcher) and the
// inbox page (live list, watched conversation). Members without inbox access get no socket.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { API_URL, useAuth } from "@/lib/auth-context";
import { connectInbox } from "@/lib/inbox-socket";
import { createNotifier } from "@/lib/notify";
import { PERMISSIONS, canAny } from "@/lib/permissions";

export const VIEW_INBOX = [PERMISSIONS.QUERIES_ANSWER, PERMISSIONS.ANALYTICS_VIEW, PERMISSIONS.BOT_CONFIGURE];
const COUNTS_DEBOUNCE_MS = 750;

const RealtimeContext = createContext(null);

// null outside the provider or without inbox access.
export function useInboxRealtime() {
  return useContext(RealtimeContext);
}

export function InboxRealtimeProvider({ orgId, membership, children }) {
  const enabled = canAny(membership, VIEW_INBOX);
  return enabled ? (
    <Connected key={orgId} orgId={orgId}>
      {children}
    </Connected>
  ) : (
    children
  );
}

function Connected({ orgId, children }) {
  const { api, getFreshToken } = useAuth();
  const router = useRouter();
  const [counts, setCounts] = useState(null);
  const [membershipId, setMembershipId] = useState(null);
  const [connection, setConnection] = useState("connecting"); // connecting | live | reconnecting | closed
  const [presence, setPresence] = useState({}); // membership id -> agent_status (newest)
  const [unread, setUnread] = useState([]); // unread notifications, newest first
  const [notifier, setNotifier] = useState(null);
  const [callOffer, setCallOffer] = useState(null); // the transfer ringing this member's softphone: {transfer_id, conversation_id}
  const me = useRef(null); // own membership id, for call offers
  const presenceAt = useRef({}); // membership id -> newest `at` seen; older events are stale
  const listeners = useRef(new Set());
  const socket = useRef(null);
  const countsTimer = useRef(null);
  const routerRef = useRef(router);
  useEffect(() => {
    routerRef.current = router;
  });

  const subscribe = useCallback((fn) => {
    listeners.current.add(fn);
    return () => listeners.current.delete(fn);
  }, []);

  const refreshCounts = useCallback(() => {
    clearTimeout(countsTimer.current);
    countsTimer.current = setTimeout(() => {
      api
        .get(`/orgs/${orgId}/inbox/counts`)
        .then(setCounts)
        .catch(() => {});
    }, COUNTS_DEBOUNCE_MS);
  }, [api, orgId]);

  const loadUnread = useCallback(() => {
    api
      .get(`/orgs/${orgId}/notifications?unread_only=true`)
      .then(setUnread)
      .catch(() => {}); // a platform admin who is not a member has none (403)
  }, [api, orgId]);

  useEffect(() => {
    const n = createNotifier({
      toast: (title, options) => toast(title, options),
      onOpen: (item) => routerRef.current.push(`/orgs/${orgId}/inbox/${item.conversation_id}`),
    });
    // The notifier needs window, so it is made after mount.
    setNotifier(n);
    loadUnread();

    function onEvent(event) {
      switch (event.type) {
        case "ready":
          setCounts(event.counts);
          setMembershipId(event.membership_id);
          me.current = event.membership_id;
          setConnection("live");
          break;
        case "reconnected":
          loadUnread();
          break;
        case "reconnecting":
          setConnection("reconnecting");
          break;
        case "closed":
          setConnection("closed");
          break;
        case "conversation.upsert":
          refreshCounts();
          break;
        case "presence":
          if (typeof event.at === "number" && event.at < (presenceAt.current[event.membership_id] ?? 0)) break;
          if (typeof event.at === "number") presenceAt.current[event.membership_id] = event.at;
          setPresence((cur) => ({ ...cur, [event.membership_id]: event.agent_status }));
          break;
        case "call_offer":
          if (event.membership_id === me.current) setCallOffer(event);
          break;
        case "call_offer_ended":
          if (event.membership_id === me.current) {
            setCallOffer((cur) => (cur && cur.transfer_id === event.transfer_id ? null : cur));
          }
          break;
        case "notification":
          if (event.notification) {
            n.notify(event.notification);
            setUnread((cur) => [event.notification, ...cur.filter((x) => x.id !== event.notification.id)]);
          }
          break;
        default:
          break;
      }
      for (const fn of listeners.current) fn(event);
    }

    const before = () => new Date(Date.now() + 24 * 3600 * 1000).toISOString();
    socket.current = connectInbox({
      apiUrl: API_URL,
      orgId,
      getToken: getFreshToken,
      onEvent,
      client: {
        messages: (id) =>
          api
            .get(`/orgs/${orgId}/inbox/conversations/${id}/messages?before=${encodeURIComponent(before())}&limit=100`)
            .then((page) => page.items),
      },
    });
    return () => {
      clearTimeout(countsTimer.current);
      socket.current?.close();
      socket.current = null;
    };
  }, [api, orgId, getFreshToken, refreshCounts, loadUnread]);

  const watch = useCallback((id) => socket.current?.watch(id), []);
  const unwatch = useCallback(() => socket.current?.unwatch(), []);

  const markRead = useCallback(
    async (ids) => {
      await api.post(`/orgs/${orgId}/notifications/read`, { ids });
      setUnread((cur) => (ids === "all" ? [] : cur.filter((x) => !ids.includes(x.id))));
    },
    [api, orgId],
  );

  const value = useMemo(
    () => ({
      counts,
      membershipId,
      connection,
      presence,
      unread,
      notifier,
      callOffer,
      subscribe,
      watch,
      unwatch,
      refreshCounts,
      markRead,
    }),
    [counts, membershipId, connection, presence, unread, notifier, callOffer, subscribe, watch, unwatch, refreshCounts, markRead],
  );

  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>;
}
