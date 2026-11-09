// Pure helpers for the risk rules page. Mirrors RiskRuleService; the server stays the source of truth.

export const ACTIONS = [
  { value: "escalate", label: "Hand to a person" },
  { value: "offer_human", label: "Offer a person" },
  { value: "bot_handles", label: "Bot handles it" },
];
export const actionLabel = (value) => ACTIONS.find((a) => a.value === value)?.label ?? value;

export const BUILTIN_LABELS = {
  asks_for_human: "Customer asks for a person",
  low_confidence: "Low confidence answer",
  frustration: "Customer sounds frustrated",
  repeated_failure: "Repeated failed answers",
  write_action: "Bot tries to change data",
};

export const MAX_NAME_LENGTH = 100;
export const MAX_KEYWORDS = 30;
export const MIN_KEYWORD_LENGTH = 2;
export const MAX_KEYWORD_LENGTH = 100;
export const MAX_FAILURES = 20;

// Which controls of a rule row are usable. A locked rule (write_action) can't be disabled,
// deleted or have its action changed; built-in rules can't be deleted; only topics have keywords.
export function ruleControls(rule) {
  const locked = Boolean(rule.locked);
  return {
    action: !locked,
    enabled: !locked,
    remove: !locked && rule.kind === "topic",
    keywords: !locked && rule.kind === "topic",
  };
}

// The id order after moving the item at `index` by `delta` (-1 up, +1 down); null when it can't move.
export function moveId(ids, index, delta) {
  const to = index + delta;
  if (index < 0 || index >= ids.length || to < 0 || to >= ids.length) return null;
  const next = [...ids];
  [next[index], next[to]] = [next[to], next[index]];
  return next;
}

export function parseKeywords(text) {
  return [...new Set(text.split(/[,\n]/).map((k) => k.trim()).filter(Boolean))];
}

// -> { ok, errors: { name?, keywords?, action? } }
export function validateTopicRule({ name, keywords, action }) {
  const errors = {};
  const trimmed = (name ?? "").trim();
  if (!trimmed || trimmed.length > MAX_NAME_LENGTH) errors.name = `Name must be 1-${MAX_NAME_LENGTH} characters.`;
  if (keywords.length > MAX_KEYWORDS) errors.keywords = `At most ${MAX_KEYWORDS} keywords.`;
  else if (keywords.some((k) => k.length < MIN_KEYWORD_LENGTH || k.length > MAX_KEYWORD_LENGTH)) {
    errors.keywords = `Each keyword must be ${MIN_KEYWORD_LENGTH}-${MAX_KEYWORD_LENGTH} characters.`;
  }
  if (!ACTIONS.some((a) => a.value === action)) errors.action = "Choose an action.";
  return { ok: Object.keys(errors).length === 0, errors };
}

// -> error message or null, for the editable params of low_confidence / repeated_failure.
export function validateParam(builtinKey, value) {
  if (builtinKey === "low_confidence") {
    return typeof value === "number" && value >= 0 && value <= 1 ? null : "Enter a number from 0 to 1.";
  }
  if (builtinKey === "repeated_failure") {
    return Number.isInteger(value) && value >= 1 && value <= MAX_FAILURES
      ? null
      : `Enter a whole number from 1 to ${MAX_FAILURES}.`;
  }
  return null;
}
