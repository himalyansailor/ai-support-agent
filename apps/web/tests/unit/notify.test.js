import { describe, it, expect, vi } from "vitest";
import { createNotifier, notificationTitle } from "@/lib/notify";

function fakeNotification(permission = "default") {
  const created = [];
  function N(title, options) {
    created.push({ title, options });
    this.close = () => {};
  }
  N.permission = permission;
  N.requestPermission = vi.fn(async () => {
    N.permission = "granted";
    return "granted";
  });
  N.created = created;
  return N;
}

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
  };
}

const NOTE = { id: "n1", kind: "assigned", conversation_id: "c1", actor_name: "Bo", customer_name: "Ann" };

function setup({ permission = "default", hidden = false, storage = memoryStorage() } = {}) {
  const NotificationImpl = fakeNotification(permission);
  const doc = { hidden };
  const toast = vi.fn();
  const playSound = vi.fn();
  const notifier = createNotifier({ NotificationImpl, documentRef: doc, storage, toast, playSound });
  return { notifier, NotificationImpl, doc, toast, playSound, storage };
}

describe("notify", () => {
  it("no permission request without user action", async () => {
    const { notifier, NotificationImpl, toast } = setup({ hidden: true });
    notifier.notify(NOTE);
    expect(NotificationImpl.requestPermission).not.toHaveBeenCalled();
    expect(NotificationImpl.created).toEqual([]);
    expect(toast).toHaveBeenCalledTimes(1);
    expect(notifier.desktopState()).toBe("default");

    // the "Enable desktop alerts" click
    await notifier.enableDesktopAlerts();
    expect(NotificationImpl.requestPermission).toHaveBeenCalledTimes(1);
    expect(notifier.desktopState()).toBe("enabled");
  });

  it("background only browser notification", () => {
    const { notifier, NotificationImpl, doc, toast, playSound } = setup({ permission: "granted" });
    notifier.notify(NOTE);
    expect(NotificationImpl.created).toEqual([]);
    expect(playSound).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledTimes(1);

    doc.hidden = true;
    notifier.notify(NOTE);
    expect(NotificationImpl.created).toHaveLength(1);
    expect(NotificationImpl.created[0].title).toBe("Assigned to you: Ann");
    expect(playSound).toHaveBeenCalledTimes(1);
    expect(toast).toHaveBeenCalledTimes(2); // in-app toasts always show
  });

  it("remembers the opt-out", () => {
    const storage = memoryStorage();
    const first = setup({ permission: "granted", hidden: true, storage });
    first.notifier.disableDesktopAlerts();
    first.notifier.notify(NOTE);
    expect(first.NotificationImpl.created).toEqual([]);
    expect(first.playSound).not.toHaveBeenCalled();

    const again = setup({ permission: "granted", hidden: true, storage });
    expect(again.notifier.desktopState()).toBe("disabled");
    again.notifier.notify(NOTE);
    expect(again.NotificationImpl.created).toEqual([]);
  });

  it("survives storage that throws and a browser without notifications", () => {
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
      removeItem: () => {
        throw new Error("blocked");
      },
    };
    const toast = vi.fn();
    const notifier = createNotifier({ NotificationImpl: undefined, documentRef: { hidden: true }, storage: broken, toast });
    expect(notifier.desktopState()).toBe("unsupported");
    expect(() => notifier.disableDesktopAlerts()).not.toThrow();
    notifier.notify(NOTE);
    expect(toast).toHaveBeenCalledTimes(1);
  });

  it("titles come from the kind and the customer name", () => {
    expect(notificationTitle({ kind: "mentioned", customer_name: "Ann" })).toBe("You were mentioned: Ann");
    expect(notificationTitle({ kind: "sla_breached", customer_name: "Visitor" })).toBe("SLA breached: Visitor");
  });

  it("survives a localStorage getter that throws (site data blocked)", () => {
    const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("The operation is insecure.", "SecurityError");
      },
    });
    try {
      const toast = vi.fn();
      let notifier;
      expect(() => {
        notifier = createNotifier({ NotificationImpl: fakeNotification("granted"), documentRef: { hidden: true }, toast, playSound: vi.fn() });
      }).not.toThrow();
      expect(notifier.desktopState()).toBe("enabled");
      expect(() => notifier.disableDesktopAlerts()).not.toThrow();
      expect(notifier.desktopState()).toBe("disabled"); // remembered for this page
      notifier.notify(NOTE);
      expect(toast).toHaveBeenCalledTimes(1);
    } finally {
      if (original) Object.defineProperty(globalThis, "localStorage", original);
      else delete globalThis.localStorage;
    }
  });
});
