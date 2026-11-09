"use client";

import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "@/components/inline-error";

// GET `path` once (again after reload()). The previous data stays while a reload is in flight (`loading`), so a
// refresh does not blank the page; `error` belongs to the latest attempt only.
export function useAdminGet(api, path, enabled = true) {
  const [state, setState] = useState({ data: null, error: null, settled: 0 });
  const [attempt, setAttempt] = useState(1);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    api
      .get(path)
      .then((data) => !cancelled && setState({ data, error: null, settled: attempt }))
      .catch((err) => !cancelled && setState((s) => ({ data: s.data, error: errorMessage(err), settled: attempt })));
    return () => {
      cancelled = true;
    };
  }, [api, path, enabled, attempt]);
  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  return { data: state.data, error: state.error, loading: enabled && state.settled !== attempt, reload };
}
