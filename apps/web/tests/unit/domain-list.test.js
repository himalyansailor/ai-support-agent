import { describe, it, expect, vi } from "vitest";
import { createElement, act, useState } from "react";
import { createRoot } from "react-dom/client";
import { DomainList } from "@/components/widgets/domain-list";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

function Harness({ onSave }) {
  const [domains, setDomains] = useState([]);
  return createElement(
    "form",
    { onSubmit: (e) => { e.preventDefault(); onSave(domains); } },
    createElement(DomainList, { domains, onChange: setDomains }),
  );
}

async function setup() {
  const onSave = vi.fn();
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(createElement(Harness, { onSave })));
  const input = container.querySelector("#domain-input");
  async function type(value) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    await act(async () => {
      setter.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }
  return { container, input, onSave, type };
}

describe("DomainList inside a form", () => {
  it("adds with Enter without submitting the outer form", async () => {
    const { container, input, onSave, type } = await setup();
    await type("Shop.Example.com");
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    });
    expect(container.textContent).toContain("shop.example.com");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("adds with the Add button without submitting the outer form", async () => {
    const { container, onSave, type } = await setup();
    await type("*.example.com");
    const add = [...container.querySelectorAll("button")].find((b) => b.textContent === "Add");
    await act(async () => add.click());
    expect(container.textContent).toContain("*.example.com");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("shows an error for an invalid entry", async () => {
    const { container, type } = await setup();
    await type("bad entry");
    const add = [...container.querySelectorAll("button")].find((b) => b.textContent === "Add");
    await act(async () => add.click());
    expect(container.querySelector("[role=alert]")).toBeTruthy();
  });
});
