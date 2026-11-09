import { describe, it, expect, vi, afterEach } from "vitest";
import {
  mailboxPayload,
  validateMailboxForm,
  defaultPorts,
  nextPort,
  connectionStatusLabel,
  emptyMailboxForm,
  formFromMailbox,
  mailErrorMessage,
  buildSrcdoc,
  splitQuoted,
  applyEmailStatus,
  deliveryDisplay,
  downloadAttachment,
  smtpStatusLabel,
  errorTypeLabel,
  replyRecipient,
  recipientDiffers,
  mergeOwnReply,
} from "@/lib/email";
import { INTEGRATION_ERROR_KINDS } from "@/lib/analytics";
import { ApiError } from "@/lib/api";

const saved = {
  id: "m1",
  address: "help@example.com",
  display_name: "Help",
  imap_host: "imap.example.com",
  imap_port: 993,
  imap_security: "ssl",
  imap_username: "help",
  imap_password_set: true,
  imap_folder: "INBOX",
  smtp_host: "smtp.example.com",
  smtp_port: 587,
  smtp_security: "starttls",
  smtp_username: "help",
  smtp_password_set: true,
  enabled: true,
  status: "active",
  last_error_type: null,
};

const filled = () => ({
  ...emptyMailboxForm(),
  address: "help@example.com",
  imap_host: "imap.example.com",
  imap_username: "help",
  imap_password: "pw",
  smtp_host: "smtp.example.com",
  smtp_username: "help",
  smtp_password: "pw",
});

describe("mailboxPayload", () => {
  it("omits blank passwords when already set", () => {
    const form = { ...formFromMailbox(saved) };
    const payload = mailboxPayload(form);
    expect(payload).not.toHaveProperty("imap_password");
    expect(payload).not.toHaveProperty("smtp_password");
    expect(payload.imap_port).toBe(993);
  });

  it("sends typed passwords", () => {
    const payload = mailboxPayload({ ...formFromMailbox(saved), smtp_password: "new" });
    expect(payload.smtp_password).toBe("new");
    expect(payload).not.toHaveProperty("imap_password");
  });
});

describe("validateMailboxForm", () => {
  it("requires passwords only when new", () => {
    const blank = { ...filled(), imap_password: "", smtp_password: "" };
    expect(Object.keys(validateMailboxForm(blank, { isNew: true }))).toEqual(["imap_password", "smtp_password"]);
    expect(validateMailboxForm(formFromMailbox(saved), { isNew: false, existing: saved })).toEqual({});
  });

  it("requires the password again on a side whose server settings changed", () => {
    const form = { ...formFromMailbox(saved), imap_host: "other.example.com" };
    const errors = validateMailboxForm(form, { isNew: false, existing: saved });
    expect(Object.keys(errors)).toEqual(["imap_password"]);
    expect(validateMailboxForm({ ...form, imap_password: "x" }, { existing: saved })).toEqual({});
    for (const change of [{ smtp_port: "2525" }, { smtp_username: "x" }, { smtp_security: "ssl" }]) {
      expect(Object.keys(validateMailboxForm({ ...formFromMailbox(saved), ...change }, { existing: saved }))).toEqual([
        "smtp_password",
      ]);
    }
  });

  it("checks formats and bounds", () => {
    const errors = validateMailboxForm(
      { ...filled(), address: "nope", imap_host: "bad host", smtp_port: "70000", imap_folder: "", display_name: "x".repeat(101) },
      { isNew: true },
    );
    expect(Object.keys(errors).sort()).toEqual(["address", "display_name", "imap_folder", "imap_host", "smtp_port"]);
    expect(validateMailboxForm(filled(), { isNew: true })).toEqual({});
  });
});

describe("defaultPorts", () => {
  it("by security", () => {
    expect(defaultPorts("ssl", "imap")).toBe(993);
    expect(defaultPorts("starttls", "imap")).toBe(143);
    expect(defaultPorts("none", "imap")).toBe(143);
    expect(defaultPorts("ssl", "smtp")).toBe(465);
    expect(defaultPorts("starttls", "smtp")).toBe(587);
    expect(defaultPorts("none", "smtp")).toBe(25);
  });
});

describe("nextPort", () => {
  it("moves a default port with the security change", () => {
    expect(nextPort("993", "ssl", "starttls", "imap")).toBe("143");
    expect(nextPort("587", "starttls", "ssl", "smtp")).toBe("465");
  });

  it("keeps a custom port", () => {
    expect(nextPort("2525", "starttls", "ssl", "smtp")).toBe("2525");
    expect(nextPort("1993", "ssl", "none", "imap")).toBe("1993");
  });
});

describe("connectionStatusLabel", () => {
  it("maps auth_failed to destructive", () => {
    expect(connectionStatusLabel({ ...saved, status: "auth_failed" }).tone).toBe("destructive");
    expect(connectionStatusLabel(saved)).toEqual({ label: "Connected", tone: "success" });
    expect(connectionStatusLabel(null).tone).toBe("muted");
  });
});

describe("smtpStatusLabel", () => {
  it("reports the sending side on its own", () => {
    expect(smtpStatusLabel({ ...saved, smtp_status: "active" })).toEqual({ label: "Connected", tone: "success" });
    const smtpDown = { ...saved, smtp_status: "auth_failed", smtp_error_type: "auth_failed" };
    expect(smtpStatusLabel(smtpDown)).toEqual({ label: "Sign-in failed", tone: "destructive" });
    expect(connectionStatusLabel(smtpDown)).toEqual({ label: "Connected", tone: "success" }); // receiving is fine
    expect(smtpStatusLabel({ ...saved, smtp_status: "error" }).tone).toBe("destructive");
    expect(smtpStatusLabel({ ...saved, enabled: false, smtp_status: "active" }).label).toBe("Connected"); // pause stops sync only
    expect(smtpStatusLabel(null).tone).toBe("muted");
    expect(errorTypeLabel("address_not_allowed")).toBe("The server address is not allowed");
  });
});

describe("reply recipient", () => {
  const inbound = (id, extra) => ({ id, email: { direction: "inbound", from_address: "ann@x.co", reply_to: null, ...extra } });
  it("is the newest inbound email's Reply-To, else its From, else the contact email", () => {
    expect(replyRecipient([], "ann@x.co")).toBe("ann@x.co");
    expect(replyRecipient([inbound("1")], "ann@x.co")).toBe("ann@x.co");
    const messages = [
      inbound("1", { reply_to: "old@x.co" }),
      inbound("2", { from_address: "colleague@x.co" }),
      { id: "3", email: { direction: "outbound", from_address: "help@us.co", to: ["colleague@x.co"] } },
      { id: "4", email: null },
    ];
    expect(replyRecipient(messages, "ann@x.co")).toBe("colleague@x.co");
    expect(replyRecipient([...messages, inbound("5", { reply_to: "evil@y.co" })], "ann@x.co")).toBe("evil@y.co");
  });

  it("differs only when another address, ignoring case", () => {
    expect(recipientDiffers("evil@y.co", "ann@x.co")).toBe(true);
    expect(recipientDiffers("Ann@X.co", "ann@x.co")).toBe(false);
    expect(recipientDiffers(null, "ann@x.co")).toBe(false);
    expect(recipientDiffers("ann@x.co", null)).toBe(false);
  });
});

describe("mergeOwnReply", () => {
  const msg = (status) => ({ id: "r1", created_at: "2026-01-01T00:00:00Z", email: { delivery_status: status, error_type: null } });
  it("keeps a status already past queued", () => {
    expect(mergeOwnReply([msg("sent")], msg("queued"))[0].email.delivery_status).toBe("sent");
    expect(mergeOwnReply([msg("sending")], msg("queued"))[0].email.delivery_status).toBe("sending");
    expect(mergeOwnReply([msg("queued")], msg("queued"))[0].email.delivery_status).toBe("queued");
    expect(mergeOwnReply([], msg("queued"))).toHaveLength(1);
    const plain = { id: "r2", created_at: "2026-01-01T00:00:01Z", email: null };
    expect(mergeOwnReply([msg("sent")], plain).map((m) => m.id)).toEqual(["r1", "r2"]);
  });
});

describe("mailErrorMessage", () => {
  it("is friendly on 429", () => {
    expect(mailErrorMessage(new ApiError(429, "rate limited"))).toMatch(/wait/i);
    expect(mailErrorMessage(new ApiError(422, "bad host"))).toBe("bad host");
  });
});

describe("integration error kinds", () => {
  it("include email", () => {
    expect(INTEGRATION_ERROR_KINDS).toContain("email");
  });
});

describe("email rendering helpers", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const csp = (doc) => /content="([^"]*)"/.exec(doc)[1];
  const imgSrc = (doc) => /img-src ([^;]*);/.exec(csp(doc))[1];

  it("buildSrcdoc blocks remote images until loadImages", () => {
    expect(imgSrc(buildSrcdoc("<p>x</p>", {}))).toBe("data:");
    expect(csp(buildSrcdoc("<p>x</p>", {}))).toBe("default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; base-uri 'none'; form-action 'none'");
    expect(buildSrcdoc("<p>x</p>").startsWith("<!doctype html><meta http-equiv=\"Content-Security-Policy\"")).toBe(true);
    expect(buildSrcdoc("<p>x</p>")).toContain('<base target="_blank">');
    expect(imgSrc(buildSrcdoc("<p>x</p>", { loadImages: true }))).toBe("data: https: http:");
    expect(buildSrcdoc("<p>x</p>").includes("<meta http-equiv=\"Content-Security-Policy\"")).toBe(true);
  });

  it("buildSrcdoc hides quoted blocks unless showQuoted", () => {
    expect(buildSrcdoc("<p>x</p>", { showQuoted: false })).toContain(
      "blockquote, .gmail_quote, .yahoo_quoted, #appendonsend ~ * { display:none }",
    );
    expect(buildSrcdoc("<p>x</p>", { showQuoted: true })).not.toContain("display:none");
  });

  it('splitQuoted finds "On ... wrote:" and trailing > lines', () => {
    expect(splitQuoted("Thanks!\n\nOn Mon, 5 Jan 2026, Ann <a@b.co> wrote:\n> hi\n> there")).toEqual({
      body: "Thanks!",
      quoted: "On Mon, 5 Jan 2026, Ann <a@b.co> wrote:\n> hi\n> there",
    });
    expect(splitQuoted("Thanks!\nOn Mon, 5 Jan 2026,\nAnn <a@b.co> wrote:\n> hi").body).toBe("Thanks!");
    expect(splitQuoted("Reply\n> old 1\n> old 2\n")).toEqual({ body: "Reply", quoted: "> old 1\n> old 2\n" });
    // a > run followed by more text is not trailing
    expect(splitQuoted("> a quote\nmy answer")).toEqual({ body: "> a quote\nmy answer", quoted: "" });
    expect(splitQuoted("plain")).toEqual({ body: "plain", quoted: "" });
  });

  it("applyEmailStatus updates only the matching message", () => {
    const messages = [
      { id: "a", email: { delivery_status: "queued", error_type: null } },
      { id: "b", email: { delivery_status: "queued", error_type: null } },
      { id: "c", email: null },
    ];
    const next = applyEmailStatus(messages, { message_id: "b", status: "failed", error_type: "smtp_5xx" });
    expect(next[0]).toBe(messages[0]);
    expect(next[1].email).toEqual({ delivery_status: "failed", error_type: "smtp_5xx" });
    expect(applyEmailStatus(messages, { message_id: "zzz", status: "sent" })).toBe(messages);
  });

  it("labels ambiguous and interrupted sends as uncertain", () => {
    expect(deliveryDisplay("failed", "ambiguous").label).toBe("May not have been sent");
    expect(deliveryDisplay("failed", "interrupted").label).toBe("May not have been sent");
    expect(deliveryDisplay("failed", "smtp_5xx").label).toBe("Failed");
    expect(deliveryDisplay("retry", "smtp_4xx").label).toBe("Retrying");
    expect(deliveryDisplay("sent", null).detail).toBe("");
  });

  it("downloadAttachment clicks an object URL and revokes it", async () => {
    const blob = new Blob(["x"]);
    vi.useFakeTimers();
    const create = vi.fn(() => "blob:1");
    const revoke = vi.fn();
    vi.stubGlobal("URL", { createObjectURL: create, revokeObjectURL: revoke });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const api = { blob: vi.fn().mockResolvedValue(blob) };
    await downloadAttachment(api, "/x/attachments/1", "a.pdf");
    expect(api.blob).toHaveBeenCalledWith("/x/attachments/1");
    expect(create).toHaveBeenCalledWith(blob);
    expect(click).toHaveBeenCalled();
    expect(revoke).not.toHaveBeenCalled(); // not in the same tick as the click
    vi.advanceTimersByTime(60_000);
    expect(revoke).toHaveBeenCalledWith("blob:1");
  });

  it("splitQuoted shows everything when nothing precedes the quote", () => {
    const text = "On Mon, Ann wrote:\n> hi";
    expect(splitQuoted(text)).toEqual({ body: text, quoted: "" });
  });
});
