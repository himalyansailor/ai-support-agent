import { describe, it, expect } from "vitest";
import { ApiError } from "@/lib/api";
import { credentialProviders, providerLabel } from "@/components/ai/add-credential-dialog";
import { canReplyInText, endReasonLabel, formatCallDuration, listQuery, matchesView } from "@/lib/inbox";
import {
  READINESS_MESSAGES,
  formatE164,
  modeFields,
  numberStatus,
  readinessMessages,
  voiceErrorMessage,
} from "@/lib/voice";

const REASONS = [
  "voice_disabled",
  "stt_not_configured",
  "tts_not_configured",
  "public_url_missing",
  "telephony_not_connected",
  "telephony_auth_failed",
  "no_numbers",
  "browser_calling_not_set_up",
  "realtime_not_configured",
  "transfer_off",
  "no_caller_id_number",
];

describe("voice helpers", () => {
  it("readinessMessages maps every reason", () => {
    const messages = readinessMessages(REASONS);
    expect(messages).toHaveLength(REASONS.length);
    for (const m of messages) expect(m).toMatch(/\.$/);
    expect(new Set(messages).size).toBe(REASONS.length);
    for (const r of REASONS) expect(READINESS_MESSAGES[r]).toBeTruthy();
    expect(readinessMessages([])).toEqual([]);
    expect(readinessMessages(["something_new"])).toHaveLength(1);
  });

  it("readinessMessages covers the 7b reasons", () => {
    const [rt, off, caller] = readinessMessages(["realtime_not_configured", "transfer_off", "no_caller_id_number"]);
    expect(rt).toMatch(/speech-to-speech/i);
    expect(off).toMatch(/transfer/i);
    expect(caller).toMatch(/caller ID|number/i);
    expect(new Set([rt, off, caller]).size).toBe(3);
  });

  it("modeFields switches fields", () => {
    expect(modeFields("chained")).toBe("speech");
    expect(modeFields("speech_to_speech")).toBe("realtime");
    expect(modeFields(undefined)).toBe("speech");
  });

  it("voiceErrorMessage explains the 7b team errors", () => {
    expect(voiceErrorMessage(new ApiError(422, "raw", null, "invalid_phone_number"))).toMatch(/international/i);
    expect(voiceErrorMessage(new ApiError(409, "raw", null, "agent_unreachable"))).toMatch(/phone/i);
  });

  it("agent_unreachable points the agent at My call settings", () => {
    expect(voiceErrorMessage(new ApiError(409, "raw", null, "agent_unreachable"))).toBe(
      "Add your phone number in My call settings to be called on your phone.",
    );
  });

  it("formatE164 formats NANP numbers", () => {
    expect(formatE164("+14155550123")).toBe("+1 415 555 0123");
    expect(formatE164("+442071838750")).toBe("+442071838750");
    expect(formatE164("+1415555012")).toBe("+1415555012");
    expect(formatE164(null)).toBe("");
  });

  it("numberStatus flags stale webhooks", () => {
    expect(numberStatus({ enabled: true, webhooks_out_of_date: false })).toBe("ok");
    expect(numberStatus({ enabled: true, webhooks_out_of_date: true })).toBe("webhooks_out_of_date");
    expect(numberStatus({ enabled: false, webhooks_out_of_date: true })).toBe("disabled");
  });

  it("voiceErrorMessage gives plain language for known codes", () => {
    const err = new ApiError(502, "x", null, "telephony_unavailable");
    expect(voiceErrorMessage(err)).toMatch(/Twilio/);
    expect(voiceErrorMessage(new ApiError(409, "raw", null, "unknown_code"))).toBe("raw");
    expect(voiceErrorMessage(new ApiError(409, "raw", null, "telephony_account_in_use"))).toMatch(/another organisation/);
    expect(voiceErrorMessage(new ApiError(422, "STT model not offered", null, "invalid_voice_settings"))).toBe(
      "STT model not offered",
    );
    expect(voiceErrorMessage(new Error("boom"))).toBe("Something went wrong");
  });

  it("the add-key dialog offers deepgram", () => {
    const catalog = {
      chat_models: [{ provider: "openai", requires_credential: true }],
      embedding_models: [{ provider: "openai", requires_credential: true }],
    };
    expect(credentialProviders(catalog)).toEqual(["openai", "deepgram"]);
    expect(providerLabel("deepgram")).toBe("Deepgram");
  });
});

describe("call helpers in the inbox", () => {
  it("formatCallDuration formats", () => {
    expect(formatCallDuration(75)).toBe("1:15");
    expect(formatCallDuration(3725)).toBe("1:02:05");
    expect(formatCallDuration(0)).toBe("0:00");
    expect(formatCallDuration(null)).toBe("—");
    expect(formatCallDuration(undefined)).toBe("—");
  });

  it("canReplyInText is false only for phone calls", () => {
    expect(canReplyInText({ call: { source: "phone" } })).toBe(false);
    expect(canReplyInText({ call: { source: "browser" } })).toBe(true);
    expect(canReplyInText({ call: null })).toBe(true);
    expect(canReplyInText({})).toBe(true);
  });

  it("endReasonLabel covers every reason", () => {
    const reasons = ["caller_hangup", "handoff_callback", "max_duration", "silence", "voice_unavailable", "capacity", "error"];
    const labels = reasons.map(endReasonLabel);
    expect(new Set(labels).size).toBe(reasons.length);
    for (const l of labels) expect(l).toMatch(/^[A-Z]/);
    expect(endReasonLabel("handoff_callback")).toBe("Handed to team");
    expect(endReasonLabel(null)).toBe("");
    expect(endReasonLabel("weird")).toBe("weird");
  });

  it("the Callbacks view asks for requested callbacks and matches them whatever the status", () => {
    const q = new URLSearchParams(listQuery({ view: "callbacks" }));
    expect(q.get("view")).toBe("all");
    expect(q.get("callback")).toBe("requested");
    const base = { is_test: false, status: "resolved", tags: [], assignee: null };
    expect(matchesView({ ...base, callback_status: "requested" }, { view: "callbacks" })).toBe(true);
    expect(matchesView({ ...base, callback_status: "done" }, { view: "callbacks" })).toBe(false);
    expect(matchesView({ ...base, callback_status: null }, { view: "callbacks" })).toBe(false);
  });
});
