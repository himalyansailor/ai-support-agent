// Pure helpers for the widget pages. Mirrors the backend (supportcore.widget.domains and
// WidgetService); the server stays the source of truth and its errors are always shown.

export const MAX_DOMAINS = 20;
export const MAX_GREETING_LENGTH = 500;
export const MAX_NAME_LENGTH = 100;

// -> error message, or null.
export function validateWidgetName(name) {
  const n = (name ?? "").trim();
  return n.length >= 1 && n.length <= MAX_NAME_LENGTH ? null : `Name must be 1-${MAX_NAME_LENGTH} characters.`;
}
export const POSITIONS = [
  { value: "bottom-right", label: "Bottom right" },
  { value: "bottom-left", label: "Bottom left" },
];
export const LAUNCHERS = [
  { value: "bubble", label: "Chat bubble" },
  { value: "custom", label: "My own button (JS API)" },
];

const LABEL = "[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?";
const HOST = `${LABEL}(?:\\.${LABEL})*`;
const ENTRY = new RegExp(`^(\\*\\.)?(${HOST})(?::(\\d{1,5}))?$`);
const SCHEME = /^[a-z][a-z0-9+.-]*:\/\//;

// Mirrors normalize_domain: the stored form of an allowed-domain entry, or null when invalid.
export function normalizeDomain(entry) {
  let value = String(entry ?? "").trim().toLowerCase();
  value = value.replace(SCHEME, "");
  if (value.endsWith("/")) value = value.slice(0, -1);
  const m = ENTRY.exec(value);
  if (!m) return null;
  const [, wildcard, host, port] = m;
  if (port !== undefined && !(Number(port) >= 1 && Number(port) <= 65535)) return null;
  if (wildcard && !host.includes(".")) return null; // "*.com": a wildcard over a bare TLD
  return value;
}

// -> { ok, domains, error }. Normalises every entry, drops duplicates.
export function validateDomains(entries) {
  const domains = [];
  for (const entry of entries) {
    const normalized = normalizeDomain(entry);
    if (normalized === null) {
      return {
        ok: false,
        domains: [],
        error: `"${String(entry).slice(0, 60)}" is not a valid domain. Use a host like shop.example.com, *.example.com or localhost:5500.`,
      };
    }
    if (!domains.includes(normalized)) domains.push(normalized);
  }
  if (domains.length > MAX_DOMAINS) {
    return { ok: false, domains: [], error: `You can allow at most ${MAX_DOMAINS} domains.` };
  }
  return { ok: true, domains, error: null };
}

const COLOR = /^#[0-9a-fA-F]{6}$/;
export const isColor = (v) => typeof v === "string" && COLOR.test(v);

// Only the appearance fields that differ. Blank text fields mean "unset" (null).
export function buildAppearancePatch(current, form) {
  const patch = {};
  for (const key of ["primary_color", "position", "launcher", "logo_url", "greeting"]) {
    let next = form[key];
    if (key === "logo_url" || key === "greeting") next = (next ?? "").trim() || null;
    if (next !== (current[key] ?? null)) patch[key] = next;
  }
  return patch;
}

// -> error message, or null.
export function validateAppearance(form) {
  if (!isColor(form.primary_color)) return "Colour must look like #4f46e5.";
  const logo = (form.logo_url ?? "").trim();
  if (logo && !logo.startsWith("https://")) return "The logo must be an https:// URL.";
  if ((form.greeting ?? "").trim().length > MAX_GREETING_LENGTH) {
    return `The greeting can be at most ${MAX_GREETING_LENGTH} characters.`;
  }
  return null;
}

export const JS_API_EXAMPLE = `<!-- Hide the chat bubble and use your own button -->
<script src="..." data-key="..." data-launcher="custom" async></script>

<script>
  // The loader script is async: wait for it before using window.SupportWidget.
  window.addEventListener("load", () => {
    document.getElementById("help-button").addEventListener("click", () => {
      window.SupportWidget.toggle(); // or .open() / .close()
    });

    // Identify a signed-in customer (a token your server signed, see "Identity").
    window.SupportWidget.identify(tokenFromYourServer);

    // Events: "open" and "close".
    window.SupportWidget.on("open", () => console.log("chat opened"));
  });
</script>`;

export const NODE_SIGNING_EXAMPLE = `// Node.js (npm i jsonwebtoken) - run this on YOUR server, never in the browser
import jwt from "jsonwebtoken";

const token = jwt.sign(
  { sub: user.id, email: user.email, name: user.name }, // email and name are optional
  process.env.SUPPORT_WIDGET_IDENTITY_SECRET,           // the wsec_... secret
  { algorithm: "HS256", expiresIn: "1h" },              // at most 24h
);`;

export const PYTHON_SIGNING_EXAMPLE = `# Python (pip install pyjwt) - run this on YOUR server, never in the browser
import os, time
import jwt

now = int(time.time())
token = jwt.encode(
    {"sub": str(user.id), "email": user.email, "name": user.name,  # email and name are optional
     "iat": now, "exp": now + 3600},                                # exp at most 24h after iat
    os.environ["SUPPORT_WIDGET_IDENTITY_SECRET"],                   # the wsec_... secret
    algorithm="HS256",
)`;
