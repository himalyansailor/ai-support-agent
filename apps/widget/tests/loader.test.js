import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createWidget, install } from "../src/loader.js";

const WEB = "http://localhost:3000";

function makeScript() {
  const s = document.createElement("script");
  s.src = `${WEB}/widget/v1.js`;
  return s;
}

function frameOf() {
  return document.querySelector("iframe");
}

function fromFrame(type, payload, overrides = {}) {
  const ev = new MessageEvent("message", {
    data: { source: "support-widget", type, payload },
    origin: WEB,
    source: frameOf().contentWindow,
    ...overrides,
  });
  window.dispatchEvent(ev);
}

let widget;
beforeEach(() => {
  widget = createWidget(window, makeScript());
});
afterEach(() => {
  widget.shutdown();
  document.body.innerHTML = "";
});

describe("loader", () => {
  it("creates bubble and hidden iframe from data-key", () => {
    widget.init({ key: "wk_abc" });
    const frame = frameOf();
    expect(frame.src).toBe(`${WEB}/embed/chat?key=wk_abc`);
    expect(frame.style.display).toBe("none");
    expect(frame.title).toBe("Chat");
    expect(frame.getAttribute("referrerpolicy")).toBe("strict-origin-when-cross-origin");
    expect(frame.getAttribute("allow")).toBe("microphone");
    expect(frame.hasAttribute("sandbox")).toBe(false);
    const bubble = document.querySelector("button");
    expect(bubble.getAttribute("aria-label")).toBeTruthy();
  });

  it("custom launcher has no bubble and open() shows iframe", () => {
    widget.init({ key: "wk_abc", launcher: "custom" });
    expect(document.querySelector("button")).toBeNull();
    widget.open();
    expect(frameOf().style.display).not.toBe("none");
    widget.close();
    expect(frameOf().style.display).toBe("none");
  });

  it("ignores messages from other origins", () => {
    widget.init({ key: "wk_abc" });
    const cb = vi.fn();
    widget.on("unread", cb);
    fromFrame("unread", { count: 3 }, { origin: "https://evil.example" });
    expect(cb).not.toHaveBeenCalled();
    fromFrame("unread", { count: 3 });
    expect(cb).toHaveBeenCalledWith(3);
  });

  it("ignores messages from same origin but other window", () => {
    widget.init({ key: "wk_abc" });
    const cb = vi.fn();
    widget.on("unread", cb);
    fromFrame("unread", { count: 3 }, { source: window });
    expect(cb).not.toHaveBeenCalled();
  });

  it("sends init with token via postMessage to webOrigin only", () => {
    widget.init({ key: "wk_abc", customerToken: "tok1" });
    expect(frameOf().src).not.toContain("tok1");
    const spy = vi.spyOn(frameOf().contentWindow, "postMessage");
    widget.open(); // queued until ready
    expect(spy).not.toHaveBeenCalled();
    fromFrame("ready");
    expect(spy.mock.calls[0]).toEqual([
      {
        source: "support-widget",
        type: "init",
        payload: { key: "wk_abc", customerToken: "tok1", parentOrigin: window.location.origin },
      },
      WEB,
    ]);
    expect(spy.mock.calls[1][0].type).toBe("open");
    for (const call of spy.mock.calls) expect(call[1]).toBe(WEB);
  });

  it("identify posts new token", () => {
    widget.init({ key: "wk_abc" });
    const spy = vi.spyOn(frameOf().contentWindow, "postMessage");
    fromFrame("ready");
    spy.mockClear();
    widget.identify("tok2");
    expect(spy).toHaveBeenCalledWith(
      { source: "support-widget", type: "identify", payload: { customerToken: "tok2" } },
      WEB,
    );
  });

  it("shutdown removes DOM", () => {
    widget.init({ key: "wk_abc" });
    widget.shutdown();
    expect(document.querySelector("iframe")).toBeNull();
    expect(document.querySelector("button")).toBeNull();
  });

  it("applies validated appearance config", () => {
    widget.init({ key: "wk_abc" });
    fromFrame("config", { appearance: { primary_color: "#112233", position: "bottom-left" } });
    const bubble = document.querySelector("button");
    expect(bubble.style.backgroundColor).toBe("rgb(17, 34, 51)");
    expect(bubble.style.left).toBe("20px");
    fromFrame("config", { appearance: { primary_color: "red;background:url(x)" } });
    expect(bubble.style.backgroundColor).toBe("rgb(17, 34, 51)");
  });

  it("request_close closes and Esc closes", () => {
    widget.init({ key: "wk_abc" });
    const closed = vi.fn();
    widget.on("close", closed);
    widget.open();
    fromFrame("request_close");
    expect(frameOf().style.display).toBe("none");
    widget.open();
    const host = document.createElement("input");
    document.body.appendChild(host);
    host.focus();
    host.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(frameOf().style.display).not.toBe("none");
    document.querySelector("button").focus();
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(frameOf().style.display).toBe("none");
    expect(closed).toHaveBeenCalledTimes(2);
  });

  it("identify() and open() before init() take effect after init", () => {
    widget.identify("early");
    widget.open();
    widget.init({ key: "wk_abc", customerToken: "initial" });
    expect(frameOf().style.display).not.toBe("none");
    const spy = vi.spyOn(frameOf().contentWindow, "postMessage");
    fromFrame("ready");
    expect(spy.mock.calls[0][0].payload.customerToken).toBe("early");
    expect(spy.mock.calls[1][0].type).toBe("open");
  });

  it("close() after open() before init() wins", () => {
    widget.open();
    widget.close();
    widget.init({ key: "wk_abc" });
    expect(frameOf().style.display).toBe("none");
  });

  it("install is not fooled by a non-widget window.SupportWidget", () => {
    const s = makeScript();
    document.head.appendChild(s);
    window.SupportWidget = document.createElement("div");
    install(window);
    expect(typeof window.SupportWidget.init).toBe("function");
    window.SupportWidget.shutdown();
    delete window.SupportWidget;
    s.remove();
  });
});
