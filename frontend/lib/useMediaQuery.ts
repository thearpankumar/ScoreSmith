"use client";

import { useEffect, useState } from "react";

/**
 * Client-only media query match. Defaults to `initial` for both the server render and the
 * client's first paint (the server can't know the client's viewport, so anything else
 * would risk a hydration mismatch), then corrects itself right after mount and stays in
 * sync via the query's own `change` listener.
 *
 * Trade-off: on a viewport where the real match differs from `initial` (e.g. a phone,
 * when `initial` defaults to the common desktop-testing case), there's a brief
 * post-hydration re-render as this corrects — not a hydration error, just a one-frame
 * layout adjustment.
 */
export function useMediaQuery(query: string, initial = false): boolean {
  const [matches, setMatches] = useState(initial);

  useEffect(() => {
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}
