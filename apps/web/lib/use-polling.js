import { useEffect, useRef } from "react";

// Calls fn every intervalMs while `enabled`. The next call is only scheduled after
// the previous one settles, so calls never overlap. Errors are swallowed (the
// caller's fn is responsible for surfacing them). Stops on disable and unmount.
export function usePolling(fn, { intervalMs = 2000, enabled = true } = {}) {
  const fnRef = useRef(fn);
  useEffect(() => {
    fnRef.current = fn;
  });

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    let timer = null;

    function schedule() {
      timer = setTimeout(async () => {
        try {
          await fnRef.current();
        } catch {
          // keep polling; transient errors are expected
        }
        if (!cancelled) schedule();
      }, intervalMs);
    }

    schedule();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [enabled, intervalMs]);
}
