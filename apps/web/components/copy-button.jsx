"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";

// Copies `value` to the clipboard and says so for a moment.
export function CopyButton({ value, label = "Copy", size = "sm", variant = "outline" }) {
  const [state, setState] = useState("idle");

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setState("copied");
    } catch {
      setState("failed");
    }
    setTimeout(() => setState("idle"), 2000);
  }

  return (
    <Button type="button" size={size} variant={variant} onClick={copy}>
      {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : label}
    </Button>
  );
}
