"use client";

import { useEffect, useState } from "react";

function format(seconds) {
  const m = String(Math.floor(seconds / 60)).padStart(2, "0");
  const s = String(seconds % 60).padStart(2, "0");
  return `${m}:${s}`;
}

// call: {state, reason}; strings: the widget strings; muted: boolean.
export function CallPanel({ call, strings, muted, onToggleMute, onEnd, onDismiss }) {
  const [elapsed, setElapsed] = useState(0);
  const inCall = call.state === "in_call";

  useEffect(() => {
    if (!inCall) return undefined;
    const startedAt = Date.now();
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(id);
  }, [inCall]);

  let text;
  if (call.state === "requesting_mic" || call.state === "connecting") text = strings.call_connecting;
  else if (inCall) text = strings.call_in_progress;
  else if (call.state === "ended") text = strings.call_ended;
  else text = call.reason === "mic_denied" ? strings.call_mic_denied : strings.call_failed;

  const live = call.state === "requesting_mic" || call.state === "connecting" || inCall;
  const btn = "rounded-md border border-neutral-300 px-3 py-1.5 text-sm";
  return (
    <div role="status" className="flex items-center gap-2 border-b border-neutral-200 bg-neutral-50 px-3 py-2 text-sm">
      <span className="flex-1">
        {text}
        {inCall ? <span className="ml-2 tabular-nums">{format(elapsed)}</span> : null}
      </span>
      {inCall ? (
        <button type="button" className={btn} onClick={onToggleMute}>
          {muted ? strings.call_unmute : strings.call_mute}
        </button>
      ) : null}
      {live ? (
        <button type="button" className={`${btn} bg-red-600 text-white`} onClick={onEnd}>
          {strings.call_end}
        </button>
      ) : (
        <button type="button" className={btn} onClick={onDismiss} aria-label={strings.call_dismiss}>
          ×
        </button>
      )}
    </div>
  );
}
