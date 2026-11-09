"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Mic, MicOff, PhoneOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { formatCallDuration } from "@/lib/inbox";

// The call in progress in this browser: a timer, mute, hang up and the conversation it belongs to.
export function InCallBar({ href, startedAt, muted, onToggleMute, onHangUp }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const elapsed = Math.max(0, (now - startedAt) / 1000);

  return (
    <div
      role="region"
      aria-label="Call in progress"
      className="fixed right-4 bottom-4 z-50 flex max-w-[calc(100vw-2rem)] flex-wrap items-center gap-3 rounded-lg border bg-background px-4 py-3 shadow-lg"
    >
      <span className="flex items-center gap-2 text-sm font-medium">
        <span aria-hidden="true" className="inline-block size-2 rounded-full bg-green-500" />
        On a call
        <span className="tabular-nums text-muted-foreground" aria-label="Call duration">
          {formatCallDuration(elapsed)}
        </span>
      </span>
      {href && (
        <Link href={href} className="text-sm text-primary underline-offset-4 hover:underline">
          Conversation
        </Link>
      )}
      <div className="flex gap-2">
        <Button type="button" variant="outline" size="sm" onClick={onToggleMute} aria-pressed={muted}>
          {muted ? <MicOff aria-hidden="true" /> : <Mic aria-hidden="true" />}
          {muted ? "Unmute" : "Mute"}
        </Button>
        <Button type="button" variant="destructive" size="sm" onClick={onHangUp}>
          <PhoneOff aria-hidden="true" /> Hang up
        </Button>
      </div>
    </div>
  );
}
