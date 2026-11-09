// Mirrors the backend permission keys (supportapi Permission enum).
export const PERMISSIONS = {
  ORG_MANAGE: "org.manage",
  KB_MANAGE: "kb.manage",
  BOT_CONFIGURE: "bot.configure",
  QUERIES_ANSWER: "queries.answer",
  CALLS_TAKE: "calls.take",
  TEAM_MANAGE: "team.manage",
  ANALYTICS_VIEW: "analytics.view",
  BILLING_MANAGE: "billing.manage",
  CHANNELS_MANAGE: "channels.manage",
};

export function can(membership, permissionKey) {
  return Boolean(membership?.permissions?.includes(permissionKey));
}

// True if the membership holds at least one of the given permission keys.
export function canAny(membership, permissionKeys) {
  return permissionKeys.some((key) => can(membership, key));
}
