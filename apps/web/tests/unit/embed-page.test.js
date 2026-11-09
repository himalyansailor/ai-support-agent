import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const seen = vi.hoisted(() => ({ props: [] }));
vi.mock("@/components/embed/chat-window", () => ({
  ChatWindow: (props) => {
    seen.props.push(props);
    return null;
  },
}));

vi.mock("@/lib/widget-client", () => ({
  createWidgetClient: ({ key }) => ({ key }),
}));

import EmbedChatPage from "@/app/embed/chat/page";

const PARENT = "https://shop.example.com";
let root;
let container;

function setUrl(qs) {
  window.history.pushState({}, "", `/embed/chat${qs}`);
}
function setReferrer(value) {
  Object.defineProperty(document, "referrer", { value, configurable: true });
}
async function mount() {
  container = document.createElement("div");
  root = createRoot(container);
  await act(async () => root.render(createElement(EmbedChatPage)));
}
async function post(data, { origin = PARENT, source = window.parent } = {}) {
  await act(async () => {
    window.dispatchEvent(new MessageEvent("message", { data, origin, source }));
  });
}
const init = (payload = {}) => ({
  source: "support-widget",
  type: "init",
  payload: { key: "wk_payload", customerToken: "ct", parentOrigin: "https://claimed.example.com", ...payload },
});
const last = () => seen.props[seen.props.length - 1];

describe("embed chat page message gate", () => {
  beforeEach(() => {
    seen.props = [];
    setUrl("?key=wk_url");
    setReferrer(PARENT + "/page");
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    setReferrer("");
  });

  it("accepts init from the parent and uses the verified event origin as parent_origin", async () => {
    await mount();
    await post(init());
    expect(last().boot).toMatchObject({ parentOrigin: PARENT, customerToken: "ct", preview: false });
  });

  it("rejects messages whose source is not window.parent", async () => {
    await mount();
    await post(init(), { source: null });
    expect(seen.props).toHaveLength(0);
  });

  it("rejects messages from an origin other than the embedding page", async () => {
    await mount();
    await post(init(), { origin: "https://evil.example.com" });
    expect(seen.props).toHaveLength(0);
  });

  it("in preview accepts init only from the page's own origin", async () => {
    setUrl("?key=wk_url&preview=1");
    setReferrer("");
    await mount();
    await post(init({ previewTicket: "tkt" }), { origin: PARENT });
    expect(seen.props).toHaveLength(0);
    await post(init({ previewTicket: "tkt" }), { origin: window.location.origin });
    expect(last().boot).toMatchObject({
      parentOrigin: window.location.origin,
      preview: true,
      previewTicket: "tkt",
    });
  });

  it("the URL key wins over the payload key", async () => {
    await mount();
    await post(init());
    expect(last().client.key).toBe("wk_url");
  });

  it("uses the payload key only when the URL has none", async () => {
    setUrl("");
    await mount();
    await post(init());
    expect(last().client.key).toBe("wk_payload");
  });

  it("accepts init only once", async () => {
    await mount();
    await post(init());
    const first = last().boot;
    await post(init({ customerToken: "other" }));
    expect(last().boot).toBe(first);
  });
});
