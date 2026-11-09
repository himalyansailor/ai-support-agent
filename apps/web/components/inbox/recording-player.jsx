"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { InlineError, errorMessage } from "@/components/inline-error";

// The recording of a call. Nothing is downloaded until "Play recording" is pressed; then the bytes come
// through the authenticated client as a blob (never a bare link) and play in an <audio> element. The object
// URL is revoked when the conversation changes or the player goes away.
export function RecordingPlayer({ api, url }) {
  const [src, setSrc] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const current = useRef(null);

  // On unmount or when the url changes, forget the in-flight fetch so a late result is ignored.
  useEffect(
    () => () => {
      current.current = null; // a fetch still running is ignored
    },
    [url],
  );
  useEffect(() => {
    if (!src) return undefined;
    return () => URL.revokeObjectURL(src);
  }, [src]);

  async function load() {
    if (loading) return;
    setLoading(true);
    setError(null);
    const token = {};
    current.current = token;
    try {
      const blob = await api.blob(url);
      if (current.current !== token) return;
      setSrc(URL.createObjectURL(blob));
    } catch (err) {
      if (current.current === token) setError(errorMessage(err));
    } finally {
      if (current.current === token) setLoading(false);
    }
  }

  return (
    <div className="flex flex-col gap-1">
      {src ? (
        <audio controls autoPlay preload="none" src={src} aria-label="Call recording" className="w-full" />
      ) : (
        <Button type="button" variant="outline" size="sm" className="self-start" onClick={load} disabled={loading}>
          {loading ? "Loading recording..." : "Play recording"}
        </Button>
      )}
      <InlineError message={error} />
    </div>
  );
}
