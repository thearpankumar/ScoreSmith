"use client";

import { useEffect, useState } from "react";

/**
 * `useState` that persists to `localStorage` under `key` — for per-viewer UI preferences
 * (collapsed/expanded panels, resized widths) that should survive a page reload but are
 * NOT app data (never written through the API, never shared between viewers).
 *
 * SSR-safe: `localStorage` doesn't exist on the server, and reading it synchronously
 * during the client's first render would make that render disagree with the
 * server-rendered HTML (a hydration mismatch). So this always renders `initial` for both
 * the server and the client's first paint, then syncs from `localStorage` in an effect
 * right after mount — a one-frame correction, not a mismatch, per React's documented
 * pattern for browser-only state.
 */
export function usePersistedState<T>(key: string, initial: T): [T, (value: T) => void] {
  const [state, setState] = useState<T>(initial);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(key);
      if (raw !== null) setState(JSON.parse(raw) as T);
    } catch {
      // localStorage unavailable (private browsing, disabled, quota) — keep `initial`.
    }
    setHydrated(true);
  }, [key]);

  useEffect(() => {
    // Skip the very first commit (before the read above has run) so we never clobber a
    // stored value with the SSR-default `initial` before it's had a chance to load.
    if (!hydrated) return;
    try {
      window.localStorage.setItem(key, JSON.stringify(state));
    } catch {
      // Ignore write failures (quota, private browsing).
    }
  }, [key, state, hydrated]);

  return [state, setState];
}
