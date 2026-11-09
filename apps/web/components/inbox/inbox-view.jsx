"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS, can } from "@/lib/permissions";
import {
  appendPage,
  listQuery,
  matchesView,
  mergePage,
  mergeUpsert,
  MIN_SEARCH_LENGTH,
  shortcutAllowed,
} from "@/lib/inbox";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";
import { VIEW_INBOX, useInboxRealtime } from "@/components/inbox/realtime-provider";
import { ViewList } from "@/components/inbox/view-list";
import { ConversationList } from "@/components/inbox/conversation-list";
import { ConversationPane } from "@/components/inbox/conversation-pane";
import { useNow } from "@/components/inbox/sla-badge";

const SEARCH_DEBOUNCE_MS = 300;
const NAV_THROTTLE_MS = 150; // j/k held down: at most one move per 150 ms
const CONNECTION_LABELS = { reconnecting: "Reconnecting...", closed: "Live updates stopped. Reload the page." };

// The three-column inbox: views and filters, the live conversation list, the selected conversation.
// selectedId comes from the URL (/inbox/<id>); the inbox stays mounted while it changes (it lives in the
// inbox layout), so the list, the socket subscription and the filters survive switching conversations.
export function InboxView({ selectedId }) {
  const { orgId, membership } = useOrg();
  const { api } = useAuth();
  const router = useRouter();
  const realtime = useInboxRealtime();
  const canAnswer = can(membership, PERMISSIONS.QUERIES_ANSWER);
  const me = realtime?.membershipId ?? null;
  const now = useNow();

  const [view, setView] = useState(canAnswer ? "mine" : "all");
  const [filters, setFilters] = useState({});
  const [q, setQ] = useState("");
  const [search, setSearch] = useState(""); // the applied search (debounced, 0 or >= 2 characters)
  const [items, setItems] = useState(null);
  const [cursor, setCursor] = useState(null);
  const [error, setError] = useState(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [tags, setTags] = useState([]);
  const [agents, setAgents] = useState([]);

  useEffect(() => {
    const clean = q.trim();
    if (clean.length > 0 && clean.length < MIN_SEARCH_LENGTH) return undefined;
    const id = setTimeout(() => setSearch(clean), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [q]);

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/tags`)
      .then((res) => !cancelled && setTags(res))
      .catch(() => {});
    if (canAnswer) {
      api
        .get(`/orgs/${orgId}/agents`)
        .then((res) => !cancelled && setAgents(res))
        .catch(() => {});
    }
    return () => {
      cancelled = true;
    };
  }, [api, orgId, canAnswer]);

  const fetchPage = useCallback(
    (after) => api.get(`/orgs/${orgId}/inbox/conversations?${listQuery({ view, filters, q: search, cursor: after })}`),
    [api, orgId, view, filters, search],
  );

  // While a first page loads, live upserts are buffered and applied after it (they may be newer than the
  // page). A reload of the shown query (after a reconnect) merges the page with what is shown, keeping the
  // higher version; a new query starts from an empty list.
  const queryKey = listQuery({ view, filters, q: search });
  const loading = useRef(false);
  const buffered = useRef([]);
  const shownKey = useRef(null);
  const matches = useRef(() => true);
  useEffect(() => {
    matches.current = (summary) => matchesView(summary, { view, filters, q: search, me });
  }, [view, filters, search, me]);

  useEffect(() => {
    let cancelled = false;
    const sameQuery = shownKey.current === queryKey;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- a new query starts from an empty list
    setError(null);
    if (!sameQuery) setItems(null);
    loading.current = true;
    buffered.current = [];
    fetchPage(null)
      .then((page) => {
        if (cancelled) return;
        const pending = buffered.current;
        buffered.current = [];
        loading.current = false;
        shownKey.current = queryKey;
        setItems((cur) => mergePage(sameQuery ? cur : null, page.items, pending, matches.current));
        setCursor(page.next_cursor);
      })
      .catch((err) => {
        if (cancelled) return;
        loading.current = false;
        buffered.current = [];
        setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [fetchPage, queryKey, reloadKey]);

  // Live: merge upserts into the list (kept sorted, newest version wins); reload after a reconnect.
  const subscribe = realtime?.subscribe;
  useEffect(() => {
    if (!subscribe) return undefined;
    return subscribe((e) => {
      if (e.type === "conversation.upsert" && e.conversation) {
        if (loading.current) buffered.current.push(e.conversation);
        else setItems((cur) => (cur ? mergeUpsert(cur, e.conversation, matches.current(e.conversation)) : cur));
      } else if (e.type === "reconnected") {
        setReloadKey((k) => k + 1);
      }
    });
  }, [subscribe]);

  // j / k move through the list (never while typing), throttled.
  const lastMove = useRef(0);
  useEffect(() => {
    function onKey(e) {
      if ((e.key !== "j" && e.key !== "k") || !shortcutAllowed(e) || !items?.length) return;
      e.preventDefault();
      if (e.timeStamp - lastMove.current < NAV_THROTTLE_MS) return;
      lastMove.current = e.timeStamp;
      const index = items.findIndex((c) => c.id === selectedId);
      const next = index < 0 ? 0 : Math.min(items.length - 1, Math.max(0, index + (e.key === "j" ? 1 : -1)));
      const id = items[next].id;
      if (id !== selectedId) {
        router.push(`/orgs/${orgId}/inbox/${id}`);
        requestAnimationFrame(() =>
          document.querySelector(`[data-conversation-id="${id}"]`)?.scrollIntoView({ block: "nearest" }),
        );
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, selectedId, router, orgId]);

  async function loadMore() {
    setLoadingMore(true);
    try {
      const page = await fetchPage(cursor);
      setItems((cur) => appendPage(cur, page.items));
      setCursor(page.next_cursor);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }

  // Picking the shown view (or the same filters) changes nothing, so nothing reloads.
  function changeView(next) {
    if (next !== view) setView(next);
  }

  function changeFilters(next) {
    if (listQuery({ view, filters: next }) !== listQuery({ view, filters })) setFilters(next);
  }

  // The server counts mine / unassigned / bot / callbacks; "mentions" is the unread mention notifications.
  const counts = realtime?.counts
    ? { ...realtime.counts, mentions: realtime.unread.filter((n) => n.kind === "mentioned").length }
    : null;

  const connection = CONNECTION_LABELS[realtime?.connection];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-semibold">Inbox</h1>
        {connection && (
          <span role="status" className="text-sm text-muted-foreground">
            {connection}
          </span>
        )}
        <span className="ml-auto hidden text-xs text-muted-foreground lg:inline">
          j/k move · r reply · n note{canAnswer ? " · e resolve" : ""}
        </span>
      </div>
      <div className="grid gap-4 lg:grid-cols-[12rem_20rem_minmax(0,1fr)]">
        <ViewList
          view={view}
          onView={changeView}
          counts={counts}
          filters={filters}
          onFilters={changeFilters}
          q={q}
          onQ={setQ}
          tags={tags}
          showMine={canAnswer}
        />
        <section aria-label="Conversation list" className="min-w-0 lg:max-h-[calc(100vh-10rem)] lg:overflow-y-auto">
          <InlineError message={error} />
          {!items ? (
            error ? null : (
              <LoadingState />
            )
          ) : items.length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">No conversations here.</p>
          ) : (
            <ConversationList
              orgId={orgId}
              items={items}
              selectedId={selectedId}
              now={now}
              hasMore={Boolean(cursor)}
              loadingMore={loadingMore}
              onLoadMore={loadMore}
            />
          )}
        </section>
        <section aria-label="Conversation" className="min-w-0 lg:max-h-[calc(100vh-10rem)] lg:overflow-y-auto">
          {selectedId ? (
            <ConversationPane
              key={selectedId}
              orgId={orgId}
              conversationId={selectedId}
              canAnswer={canAnswer}
              me={me}
              agents={agents}
              presence={realtime?.presence ?? {}}
              tags={tags}
              onTagCreated={(tag) => setTags((cur) => [...cur, tag].sort((a, b) => a.name.localeCompare(b.name)))}
              realtime={realtime}
            />
          ) : (
            <p className="p-4 text-sm text-muted-foreground">Select a conversation.</p>
          )}
        </section>
      </div>
    </div>
  );
}

function SelectedInbox({ children }) {
  const { conversationId } = useParams();
  return (
    <RequirePermission permission={VIEW_INBOX}>
      <InboxView selectedId={typeof conversationId === "string" ? conversationId : null} />
      {children}
    </RequirePermission>
  );
}

// The inbox route's client part: the selected conversation id comes from the URL. useParams() is
// runtime-only data, hence the Suspense boundary.
export function InboxRoute({ children }) {
  return (
    <Suspense fallback={<LoadingState />}>
      <SelectedInbox>{children}</SelectedInbox>
    </Suspense>
  );
}
