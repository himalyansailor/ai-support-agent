import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import {
  TRANSITIONS,
  appendPage,
  compareActivity,
  conversationEvent,
  mergePage,
  slaState,
  allowedTransitions,
  matchesView,
  mergeMessages,
  mergeUpsert,
  mentionToken,
  renderSavedReply,
} from "@/lib/inbox";

const conv = (id, at, extra = {}) => ({
  id,
  status: "open",
  priority: "normal",
  channel: "web",
  assignee: null,
  tags: [],
  is_test: false,
  last_message_at: at,
  version: 1,
  ...extra,
});

describe("mergeUpsert", () => {
  it("mergeUpsert keeps sort and dedupes", () => {
    const items = [conv("a", "2026-01-03T00:00:00Z"), conv("b", "2026-01-02T00:00:00Z"), conv("c", "2026-01-01T00:00:00Z")];
    // c gets a new message: moves to the top, no duplicate
    let next = mergeUpsert(items, conv("c", "2026-01-04T00:00:00Z", { version: 2 }));
    expect(next.map((x) => x.id)).toEqual(["c", "a", "b"]);
    // the same version again, or an older one, is ignored
    expect(mergeUpsert(next, conv("c", "2026-01-05T00:00:00Z", { version: 2 }))).toBe(next);
    expect(mergeUpsert(next, conv("c", "2026-01-05T00:00:00Z", { version: 1 }))).toBe(next);
    // a new conversation is inserted in order
    next = mergeUpsert(next, conv("d", "2026-01-02T12:00:00Z"));
    expect(next.map((x) => x.id)).toEqual(["c", "a", "d", "b"]);
    // ties on time fall back to id, descending (the backend's keyset order)
    next = mergeUpsert(next, conv("e", "2026-01-02T12:00:00Z"));
    expect(next.map((x) => x.id)).toEqual(["c", "a", "e", "d", "b"]);
    // no longer in the view: removed
    next = mergeUpsert(next, conv("a", "2026-01-03T00:00:00Z", { version: 5 }), false);
    expect(next.map((x) => x.id)).toEqual(["c", "e", "d", "b"]);
    // unknown match: updated if listed, not added
    expect(mergeUpsert(next, conv("z", "2026-02-01T00:00:00Z"), null).map((x) => x.id)).toEqual(["c", "e", "d", "b"]);
    const updated = mergeUpsert(next, conv("b", "2026-02-01T00:00:00Z", { version: 9 }), null);
    expect(updated[0]).toMatchObject({ id: "b", version: 9 });
  });
});

describe("matchesView", () => {
  const me = "m1";
  it("mirrors the backend views and filters", () => {
    const mine = conv("a", "t", { assignee: { membership_id: me, name: "Me" } });
    expect(matchesView(mine, { view: "mine", me })).toBe(true);
    expect(matchesView({ ...mine, status: "resolved" }, { view: "mine", me })).toBe(false);
    expect(matchesView({ ...mine, status: "resolved" }, { view: "mine", me, filters: { status: "resolved" } })).toBe(true);
    expect(matchesView(mine, { view: "unassigned", me })).toBe(false);
    expect(matchesView(conv("b", "t"), { view: "unassigned", me })).toBe(true);
    expect(matchesView(conv("b", "t", { status: "bot" }), { view: "bot", me })).toBe(true);
    expect(matchesView(conv("b", "t", { status: "closed" }), { view: "all", me })).toBe(true);
    expect(matchesView(conv("b", "t", { is_test: true }), { view: "all", me })).toBe(false);
    expect(matchesView(conv("b", "t", { is_test: true }), { view: "all", me, filters: { include_test: true } })).toBe(true);
    expect(matchesView(conv("b", "t"), { view: "all", me, filters: { priority: "high" } })).toBe(false);
    expect(matchesView(conv("b", "t", { tags: [{ id: "t1", name: "x" }] }), { view: "all", me, filters: { tag_id: "t1" } })).toBe(true);
    expect(matchesView(conv("b", "t"), { view: "all", me, filters: { assignee: "none" } })).toBe(true);
    expect(matchesView(mine, { view: "all", me, filters: { assignee: "me" } })).toBe(true);
    // the client cannot tell: mentions, a search, a knowledge-base filter
    expect(matchesView(conv("b", "t"), { view: "mentions", me })).toBe(null);
    expect(matchesView(conv("b", "t"), { view: "all", me, q: "refund" })).toBe(null);
    expect(matchesView(conv("b", "t"), { view: "all", me, filters: { kb_id: "k" } })).toBe(null);
  });
});

// Parses the backend's TRANSITIONS dict, so the two tables cannot drift apart.
function backendTransitions() {
  const file = path.resolve(import.meta.dirname, "../../../../packages/core/src/supportcore/conversations/service.py");
  const source = readFileSync(file, "utf8");
  const block = source.match(/TRANSITIONS: dict\[tuple\[str, str\], str\] = \{([\s\S]*?)\n\}/)[1];
  return [...block.matchAll(/\("(\w+)", "(\w+)"\): "(\w+)"/g)].map(([, from, to, event]) => [from, to, event]);
}

describe("transitions", () => {
  it("allowedTransitions matches backend table", () => {
    const backend = backendTransitions();
    expect(backend.length).toBeGreaterThan(10);
    expect([...TRANSITIONS].sort()).toEqual([...backend].sort());

    const buttons = (status) => allowedTransitions(status).map((t) => t.event);
    expect(buttons("bot")).toEqual(["close"]); // entering the team is claim/assign, not a status button
    expect(buttons("open")).toEqual(["resolve", "hand_back", "close"]); // agent_reply is the reply itself
    expect(buttons("pending")).toEqual(["resolve", "hand_back", "close"]); // customer_message is the customer's
    expect(buttons("resolved")).toEqual(["reopen", "close"]);
    expect(buttons("closed")).toEqual([]);
    // every button is a backend transition from that status
    for (const status of ["bot", "open", "pending", "resolved", "closed"]) {
      for (const t of allowedTransitions(status)) {
        expect(backend).toContainEqual([status, t.to, t.event]);
      }
    }
  });

  it("allowedTransitions never offers hand back on an email conversation", () => {
    const emailButtons = (status) => allowedTransitions(status, "email").map((t) => t.event);
    expect(emailButtons("open")).toEqual(["resolve", "close"]);
    expect(emailButtons("pending")).toEqual(["resolve", "close"]);
    expect(emailButtons("resolved")).toEqual(["reopen", "close"]);
    expect(allowedTransitions("open", "chat").map((t) => t.event)).toContain("hand_back");
  });

  it("allowedTransitions offers hand back on a browser call but not a phone call", () => {
    expect(allowedTransitions("open", "voice", "phone").map((t) => t.event)).toEqual(["resolve", "close"]);
    expect(allowedTransitions("open", "voice", "browser").map((t) => t.event)).toContain("hand_back");
  });
});

describe("helpers", () => {
  it("mergeMessages dedupes by id and keeps time order", () => {
    const a = { id: "1", created_at: "2026-01-01T00:00:01Z" };
    const b = { id: "2", created_at: "2026-01-01T00:00:02Z" };
    const c = { id: "3", created_at: "2026-01-01T00:00:03Z" };
    expect(mergeMessages([a, c], [b, c]).map((m) => m.id)).toEqual(["1", "2", "3"]);
  });

  it("renderSavedReply fills the two placeholders once, like the backend", () => {
    expect(renderSavedReply("Hi {customer_name}, {agent_name} here. {other}", { customerName: "Ann", agentName: "Bo" })).toBe(
      "Hi Ann, Bo here. {other}",
    );
    expect(renderSavedReply("Hi {customer_name}", { customerName: "{agent_name}", agentName: "Bo" })).toBe("Hi {agent_name}");
    expect(renderSavedReply("Hi {customer_name}!", { customerName: null })).toBe("Hi !");
  });

  it("mentionToken cannot break the token", () => {
    expect(mentionToken("Ann [admin]\nX", "id-1")).toBe("@[Ann admin X](id-1)");
    expect(mentionToken("  ", "id-1")).toBe("@[Member](id-1)");
  });
});

describe("fix round 1", () => {
  const NOW = Date.parse("2026-03-04T12:00:00Z");
  const past = "2026-03-04T11:00:00Z";
  const soon = "2026-03-04T12:10:00Z";
  const later = "2026-03-04T18:00:00Z";

  it("slaState: breached comes only from the server; countdowns only while open or pending", () => {
    // a past due date the server did not flag is not "breached"
    const unflagged = slaState({ first_response_due_at: past, resolution_due_at: later, breached: [] }, NOW, "open");
    expect(unflagged.state).not.toBe("breached");
    expect(slaState({ first_response_due_at: later, resolution_due_at: later, breached: ["resolution"] }, NOW, "open")).toMatchObject({
      state: "breached",
      kinds: ["resolution"],
    });
    // a met first response: only the resolution target counts down
    const met = slaState(
      { first_response_due_at: past, resolution_due_at: later, first_response_at: past, breached: [] },
      NOW,
      "pending",
    );
    expect(met).toMatchObject({ state: "on_track", kind: "resolution", dueAt: Date.parse(later) });
    // unmet first response, nearest target
    expect(slaState({ first_response_due_at: soon, resolution_due_at: later, first_response_at: null, breached: [] }, NOW, "open")).toMatchObject({
      state: "due_soon",
      kind: "first_response",
    });
    // resolved / closed / bot: no countdown (a server breach still shows)
    for (const status of ["resolved", "closed", "bot"]) {
      expect(slaState({ first_response_due_at: soon, resolution_due_at: later, breached: [] }, NOW, status).state).toBe("none");
    }
    expect(slaState({ first_response_due_at: past, resolution_due_at: later, breached: ["first_response"] }, NOW, "resolved").state).toBe(
      "breached",
    );
  });

  it("compareActivity and mergeMessages compare instants, not strings", () => {
    // same instant ordering regardless of offset / precision spelling
    const a = { id: "a", last_message_at: "2026-01-01T10:00:00+02:00" }; // 08:00Z
    const b = { id: "b", last_message_at: "2026-01-01T09:00:00Z" };
    expect([a, b].sort(compareActivity).map((x) => x.id)).toEqual(["b", "a"]);
    const m1 = { id: "1", created_at: "2026-01-01T09:00:00.5Z" };
    const m2 = { id: "2", created_at: "2026-01-01T10:00:00+02:00" };
    expect(mergeMessages([m1], [m2]).map((m) => m.id)).toEqual(["2", "1"]);
  });

  it("mergePage keeps the higher version and applies buffered upserts", () => {
    const current = [conv("a", "2026-01-05T00:00:00Z", { version: 7 }), conv("gone", "2026-01-06T00:00:00Z", { version: 1 })];
    const page = [conv("a", "2026-01-01T00:00:00Z", { version: 3 }), conv("b", "2026-01-02T00:00:00Z", { version: 1 })];
    // a reload: the page decides membership, a newer listed version wins
    let items = mergePage(current, page);
    expect(items.map((x) => [x.id, x.version])).toEqual([
      ["a", 7],
      ["b", 1],
    ]);
    // a new query starts from nothing; buffered upserts apply after, with their match
    items = mergePage(null, page, [conv("c", "2026-01-09T00:00:00Z"), conv("b", "2026-01-03T00:00:00Z", { version: 2 })], (s) =>
      s.id === "c" ? true : null,
    );
    expect(items.map((x) => [x.id, x.version])).toEqual([
      ["c", 1],
      ["b", 2],
      ["a", 3],
    ]);
    // a stale buffered upsert does not overwrite
    items = mergePage(null, page, [conv("b", "2026-01-03T00:00:00Z", { version: 0 })]);
    expect(items.find((x) => x.id === "b").version).toBe(1);
  });

  it("appendPage keeps the higher version and the order", () => {
    const items = [conv("a", "2026-01-05T00:00:00Z", { version: 4 })];
    const next = appendPage(items, [conv("a", "2026-01-01T00:00:00Z", { version: 2 }), conv("b", "2026-01-02T00:00:00Z")]);
    expect(next.map((x) => [x.id, x.version])).toEqual([
      ["a", 4],
      ["b", 1],
    ]);
  });

  it("conversationEvent: email.status concerns the open conversation only", () => {
    const e = { type: "email.status", conversation_id: "c1", message_id: "m1", status: "sent", error_type: null };
    expect(conversationEvent(e, "c1")).toEqual({ kind: "email_status", event: e });
    expect(conversationEvent(e, "c2")).toBeNull();
  });

  it("conversationEvent: note.added refetches the watched conversation only", () => {
    expect(conversationEvent({ type: "note.added", conversation_id: "c1" }, "c1")).toEqual({ kind: "reload" });
    expect(conversationEvent({ type: "note.added", conversation_id: "c2" }, "c1")).toBe(null);
    expect(conversationEvent({ type: "reconnected" }, "c1")).toEqual({ kind: "reload" });
    expect(conversationEvent({ type: "messages", conversation_id: "c1", messages: [{ id: "m" }] }, "c1")).toEqual({
      kind: "messages",
      messages: [{ id: "m" }],
    });
    expect(conversationEvent({ type: "bot_token", conversation_id: "c1", delta: "Hi" }, "c1")).toEqual({ kind: "bot", delta: "Hi" });
    expect(conversationEvent({ type: "message", conversation_id: "c2", message: { id: "x" } }, "c1")).toBe(null);
  });
});
