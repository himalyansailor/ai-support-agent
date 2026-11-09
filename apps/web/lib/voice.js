import { ApiError } from "@/lib/api";

// Helpers and API wrappers for the voice channel settings. Mirrors supportapi.routers.voice_channel; the
// server stays the source of truth. Twilio auth tokens are write-only: the API returns only the last four
// characters and flags, and the UI never keeps a token after submit.

export const COUNTRIES = [
  { value: "US", label: "United States (US)" },
  { value: "CA", label: "Canada (CA)" },
  { value: "GB", label: "United Kingdom (GB)" },
];

export const READINESS_MESSAGES = {
  voice_disabled: "Phone and browser calls are both turned off. Turn one on under Calls.",
  stt_not_configured: "Choose a speech-to-text provider, model and API key.",
  tts_not_configured: "Choose a text-to-speech provider, model, voice and API key.",
  public_url_missing: "The server has no public voice URL, so Twilio cannot reach it. Ask your administrator to set it.",
  telephony_not_connected: "Connect your Twilio account, or buy a phone number.",
  telephony_auth_failed: "Twilio rejected the saved credentials. Reconnect your Twilio account.",
  no_numbers: "Add a phone number so customers can call you.",
  browser_calling_not_set_up: "Browser calling is not set up on the Twilio account yet. Reconnect the account to set it up.",
  realtime_not_configured: "Choose a speech-to-speech model, voice and OpenAI API key.",
  transfer_off: "Live transfers are off, so callers who ask for a person get a callback. Turn one on under Transfers.",
  no_caller_id_number: "Add a phone number so agents can be called and customers called back from it.",
};

// "chained" (speech to text -> chat bot -> text to speech) shows the speech fields; "speech_to_speech" the
// Realtime fields.
export const modeFields = (mode) => (mode === "speech_to_speech" ? "realtime" : "speech");

// reason codes -> one plain sentence each. An unknown code gets a generic line rather than being dropped.
export function readinessMessages(reasons) {
  return (reasons ?? []).map((r) => READINESS_MESSAGES[r] ?? "Voice is not ready yet. Check the settings below.");
}

// +14155550123 -> "+1 415 555 0123". Anything that is not a full NANP number is returned unchanged.
export function formatE164(e164) {
  if (!e164) return "";
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(e164);
  return m ? `+1 ${m[1]} ${m[2]} ${m[3]}` : e164;
}

// -> "ok" | "webhooks_out_of_date" | "disabled"
export function numberStatus(number) {
  if (!number.enabled) return "disabled";
  return number.webhooks_out_of_date ? "webhooks_out_of_date" : "ok";
}

export const STATUS_LABELS = { ok: "Active", webhooks_out_of_date: "Webhooks out of date", disabled: "Disabled" };

const ERROR_MESSAGES = {
  telephony_credentials_invalid: "Twilio did not accept that Account SID and Auth Token. Check them and try again.",
  telephony_not_configured: "Connect a Twilio account first.",
  telephony_account_in_use: "This Twilio account is already connected to another organisation.",
  phone_number_unavailable: "That number is no longer available. Search again and pick another.",
  telephony_unavailable: "Twilio is not responding right now. Try again in a moment.",
  invalid_phone_number: "Enter the phone number in international format, like +14155550123.",
  invalid_call_preference: "Choose browser or phone.",
  agent_unreachable: "Add your phone number in My call settings to be called on your phone.",
  callback_number_missing: "This conversation has no callback number.",
  invalid_voice_settings: null, // the server's detail names the field; show it
};

export function voiceErrorMessage(err) {
  if (!(err instanceof ApiError)) return "Something went wrong";
  if (err.status === 429) return "Too many attempts. Wait a moment and try again.";
  return ERROR_MESSAGES[err.code] ?? err.detail;
}

export const voiceSettingsHref = (orgId) => `/orgs/${orgId}/settings/voice`;

const base = (orgId) => `/orgs/${orgId}/voice`;

export const getVoiceSettings = (api, orgId) => api.get(`${base(orgId)}/settings`);
export const updateVoiceSettings = (api, orgId, body) => api.put(`${base(orgId)}/settings`, body);
export const getReadiness = (api, orgId) => api.get(`${base(orgId)}/readiness`);
export const getCatalog = (api, orgId) => api.get(`${base(orgId)}/catalog`);
export const getTelephony = (api, orgId) => api.get(`${base(orgId)}/telephony`);
export const connectTelephony = (api, orgId, accountSid, authToken) =>
  api.put(`${base(orgId)}/telephony`, { account_sid: accountSid.trim(), auth_token: authToken });
export const disconnectTelephony = (api, orgId) => api.del(`${base(orgId)}/telephony`);
export const listNumbers = (api, orgId) => api.get(`${base(orgId)}/numbers`);
export const searchNumbers = (api, orgId, country, areaCode) => {
  const q = new URLSearchParams({ country });
  if (areaCode) q.set("area_code", areaCode);
  return api.get(`${base(orgId)}/numbers/search?${q}`);
};
export const buyNumber = (api, orgId, e164, widgetId = null) =>
  api.post(`${base(orgId)}/numbers/buy`, { e164, widget_id: widgetId });
export const listAccountNumbers = (api, orgId) => api.get(`${base(orgId)}/numbers/available`);
export const connectNumber = (api, orgId, providerSid, widgetId = null) =>
  api.post(`${base(orgId)}/numbers/connect`, { provider_sid: providerSid, widget_id: widgetId });
export const updateNumber = (api, orgId, id, body) => api.patch(`${base(orgId)}/numbers/${id}`, body);
export const releaseNumber = (api, orgId, id) => api.del(`${base(orgId)}/numbers/${id}`);
export const syncWebhooks = (api, orgId) => api.post(`${base(orgId)}/numbers/sync-webhooks`);

// "$1.15 USD" style price line from a search result.
export function priceLabel(number) {
  if (!number.monthly_price) return "Price not available";
  return `${number.monthly_price}${number.price_unit ? ` ${number.price_unit}` : ""} per month`;
}

export function validateCallSettings({ recording_retention_days, max_call_minutes }) {
  const errors = {};
  const days = Number(recording_retention_days);
  const mins = Number(max_call_minutes);
  if (!Number.isInteger(days) || days < 1 || days > 3650) errors.recording_retention_days = "Enter 1 to 3650 days.";
  if (!Number.isInteger(mins) || mins < 1 || mins > 120) errors.max_call_minutes = "Enter 1 to 120 minutes.";
  return errors;
}

const agent = (orgId) => `${base(orgId)}/agent`;
export const getAgentToken = (api, orgId) => api.post(`${agent(orgId)}/token`);
export const beatSoftphone = (api, orgId) => api.put(`${agent(orgId)}/softphone`);
export const clearSoftphone = (api, orgId) => api.del(`${agent(orgId)}/softphone`);
export const getCallbackTicket = (api, orgId, conversationId) =>
  api.post(`${agent(orgId)}/callbacks/${conversationId}/ticket`);
export const callbackByPhone = (api, orgId, conversationId) =>
  api.post(`${agent(orgId)}/callbacks/${conversationId}/call`);

export const CALL_PREFERENCES = [
  { value: "browser", label: "Browser" },
  { value: "phone", label: "Phone" },
];
export const callPreferenceLabel = (value) => CALL_PREFERENCES.find((p) => p.value === value)?.label ?? "Browser";
