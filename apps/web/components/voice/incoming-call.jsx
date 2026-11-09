"use client";

import { useEffect } from "react";
import Link from "next/link";
import { Phone, PhoneOff } from "lucide-react";
import { Button } from "@/components/ui/button";

const RING_EVERY_MS = 3000;

// A short two-tone ring, repeated while the toast is shown. Browsers that block audio before a user gesture
// stay silent; the toast is still there.
function useRing() {
  useEffect(() => {
    let ctx = null;
    try {
      const AudioCtx = globalThis.AudioContext ?? globalThis.webkitAudioContext;
      if (!AudioCtx) return undefined;
      ctx = new AudioCtx();
    } catch {
      return undefined;
    }
    const ring = () => {
      try {
        ctx.resume?.();
        const gain = ctx.createGain();
        gain.gain.value = 0.08;
        gain.connect(ctx.destination);
        for (const [offset, freq] of [
          [0, 440],
          [0, 480],
          [0.6, 440],
          [0.6, 480],
        ]) {
          const osc = ctx.createOscillator();
          osc.frequency.value = freq;
          osc.connect(gain);
          osc.start(ctx.currentTime + offset);
          osc.stop(ctx.currentTime + offset + 0.4);
        }
      } catch {
        /* no audio */
      }
    };
    ring();
    const timer = setInterval(ring, RING_EVERY_MS);
    return () => {
      clearInterval(timer);
      try {
        ctx.close();
      } catch {
        /* already closed */
      }
    };
  }, []);
}

// A transferred call ringing this browser: Accept / Decline, and the conversation it belongs to.
export function IncomingCall({ href, onAccept, onDecline }) {
  useRing();
  return (
    <div
      role="alertdialog"
      aria-labelledby="incoming-call-title"
      className="fixed right-4 bottom-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-3 rounded-lg border bg-background p-4 shadow-lg"
    >
      <div className="flex flex-col gap-1">
        <p id="incoming-call-title" className="font-medium">
          Incoming call from a customer
        </p>
        {href ? (
          <Link href={href} className="text-sm text-primary underline-offset-4 hover:underline">
            Open the conversation
          </Link>
        ) : (
          <p className="text-sm text-muted-foreground">A caller asked for a person.</p>
        )}
      </div>
      <div className="flex gap-2">
        <Button type="button" className="flex-1" onClick={onAccept}>
          <Phone aria-hidden="true" /> Accept
        </Button>
        <Button type="button" variant="outline" className="flex-1" onClick={onDecline}>
          <PhoneOff aria-hidden="true" /> Decline
        </Button>
      </div>
    </div>
  );
}
