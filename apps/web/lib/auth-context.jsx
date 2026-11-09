"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { createApiClient } from "@/lib/api";

const AuthContext = createContext(null);

export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// Access token lives in memory only (module variable) -- never in web storage.
let accessToken = null;
const getAccessToken = () => accessToken;
const setAccessToken = (t) => {
  accessToken = t;
};

// Seconds until the JWT's `exp`, or null when it cannot be read. Only used to refresh early.
function secondsLeft(token) {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return typeof payload.exp === "number" ? payload.exp - Date.now() / 1000 : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [memberships, setMemberships] = useState([]);
  const [loading, setLoading] = useState(true);

  const clearSession = useCallback(() => {
    setAccessToken(null);
    setUser(null);
    setMemberships([]);
  }, []);

  const api = useMemo(
    () =>
      createApiClient({
        baseUrl: API_URL,
        getAccessToken,
        setAccessToken,
        onAuthFailure: clearSession,
      }),
    [clearSession],
  );

  // The access token for a WebSocket auth frame (sockets cannot retry a 401 the way api calls do):
  // refreshed first when it is missing or expires within 30 s.
  const getFreshToken = useCallback(async () => {
    const token = getAccessToken();
    const left = token ? secondsLeft(token) : 0;
    if (left !== null && left < 30) {
      try {
        await api.refresh();
      } catch {
        /* the socket's auth then fails with 4401 */
      }
    }
    return getAccessToken();
  }, [api]);

  const loadMe = useCallback(async () => {
    const me = await api.get("/me");
    setUser(me.user);
    setMemberships(me.memberships);
    return me.memberships;
  }, [api]);

  // Bootstrap goes through api.refresh(), which is single-flighted, so a
  // StrictMode double-mount sends only one refresh request.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await api.refresh();
        await loadMe();
      } catch {
        if (!cancelled) clearSession();
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, loadMe, clearSession]);

  const authenticate = useCallback(
    async (path, body) => {
      const data = await api.post(path, body);
      setAccessToken(data.access_token);
      setUser(data.user);
      return loadMe();
    },
    [api, loadMe],
  );

  // Refresh /me after creating or joining an organisation.
  const reloadMe = useCallback(() => loadMe(), [loadMe]);

  // Adopt a session handed back by POST /invites/accept for a new account.
  const adoptSession = useCallback(
    async (data) => {
      setAccessToken(data.access_token);
      setUser(data.user);
      return loadMe();
    },
    [loadMe],
  );

  const login = useCallback(
    (email, password) => authenticate("/auth/login", { email, password }),
    [authenticate],
  );

  const signup = useCallback(
    (email, password, name) =>
      authenticate("/auth/signup", { email, password, name }),
    [authenticate],
  );

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      clearSession();
    }
  }, [api, clearSession]);

  const value = useMemo(
    () => ({
      user,
      memberships,
      login,
      signup,
      logout,
      reloadMe,
      adoptSession,
      api,
      getFreshToken,
      loading,
    }),
    [user, memberships, login, signup, logout, reloadMe, adoptSession, api, getFreshToken, loading],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
