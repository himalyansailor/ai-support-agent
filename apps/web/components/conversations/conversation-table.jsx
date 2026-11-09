"use client";

import { Badge } from "@/components/ui/badge";

export const STATUS_LABELS = {
  bot: "Bot",
  open: "Waiting for a person",
  pending: "Waiting for the customer",
  resolved: "Resolved",
  closed: "Closed",
};

export function StatusBadge({ status }) {
  const variant = status === "open" ? "destructive" : status === "bot" ? "default" : "secondary";
  return <Badge variant={variant}>{STATUS_LABELS[status] ?? status}</Badge>;
}

export function customerLabel(customer) {
  return customer.name || customer.email || (customer.identified ? "Signed-in customer" : "Anonymous");
}

export function escalationLabel(escalation) {
  if (!escalation) return "";
  return escalation.rule_name || escalation.reason || escalation.rule_key || "";
}
