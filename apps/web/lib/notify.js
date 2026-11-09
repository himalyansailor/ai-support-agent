// Inbox notifications: an in-app toast always; a browser notification and a short sound only while the
// tab is in the background (document.hidden), only once the user has clicked "Enable desktop alerts"
// (the browser permission is never requested otherwise), and never after they turned desktop alerts off
// (remembered in localStorage). Names only: notifications never carry message or note text.

const STORAGE_KEY = "inbox.desktopAlerts";

const KIND_TITLES = {
  assigned: "Assigned to you",
  mentioned: "You were mentioned",
  sla_breached: "SLA breached",
};

export function notificationTitle(n) {
  return `${KIND_TITLES[n.kind] ?? "Notification"}: ${n.customer_name || "Visitor"}`;
}

export function notificationBody(n) {
  if (n.kind === "sla_breached") return "A response target was missed";
  return n.actor_name ? `By ${n.actor_name}` : "";
}

function read(storage) {
  try {
    return storage?.getItem(STORAGE_KEY) ?? null;
  } catch {
    return null;
  }
}

function write(storage, value) {
  try {
    if (value === null) storage?.removeItem(STORAGE_KEY);
    else storage?.setItem(STORAGE_KEY, value);
  } catch {
    /* storage blocked: the choice lasts for this page only */
  }
}

// A short beep through Web Audio (no audio file to load). Best effort.
export function beep() {
  try {
    const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    gain.gain.value = 0.05;
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.15);
    osc.onended = () => ctx.close?.();
  } catch {
    /* autoplay blocked or unsupported */
  }
}

// localStorage, or null: reading the property itself throws (SecurityError) when site data is blocked.
function defaultStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

// toast(title, {description, action}) shows the in-app toast (sonner's toast). onOpen(notification) runs
// when the user clicks the browser notification or the toast's action.
export function createNotifier({
  NotificationImpl = globalThis.Notification,
  documentRef = globalThis.document,
  storage = defaultStorage(),
  toast,
  playSound = beep,
  onOpen,
} = {}) {
  let choice = read(storage); // "on" | "off" | null
  let override = null; // used when storage is unavailable

  function current() {
    return override ?? choice;
  }

  // "unsupported" | "denied" | "disabled" | "enabled" | "default" (not asked yet)
  function desktopState() {
    if (!NotificationImpl) return "unsupported";
    if (NotificationImpl.permission === "denied") return "denied";
    if (current() === "off") return "disabled";
    if (NotificationImpl.permission === "granted" && current() !== "off") return "enabled";
    return "default";
  }

  // Only ever called from the "Enable desktop alerts" click.
  async function enableDesktopAlerts() {
    if (!NotificationImpl) return "unsupported";
    let permission = NotificationImpl.permission;
    if (permission !== "granted" && permission !== "denied") {
      try {
        permission = await NotificationImpl.requestPermission();
      } catch {
        permission = NotificationImpl.permission;
      }
    }
    if (permission === "granted") {
      choice = "on";
      override = "on";
      write(storage, "on");
    }
    return desktopState();
  }

  function disableDesktopAlerts() {
    choice = "off";
    override = "off";
    write(storage, "off");
    return desktopState();
  }

  function notify(n) {
    const title = notificationTitle(n);
    const description = notificationBody(n);
    toast?.(title, {
      description: description || undefined,
      action: onOpen ? { label: "Open", onClick: () => onOpen(n) } : undefined,
    });
    if (!documentRef?.hidden || desktopState() !== "enabled") return;
    try {
      const shown = new NotificationImpl(title, { body: description, tag: n.id });
      shown.onclick = () => {
        globalThis.focus?.();
        onOpen?.(n);
        shown.close?.();
      };
    } catch {
      /* some browsers only allow notifications from a service worker */
    }
    playSound?.();
  }

  return { notify, enableDesktopAlerts, disableDesktopAlerts, desktopState };
}
