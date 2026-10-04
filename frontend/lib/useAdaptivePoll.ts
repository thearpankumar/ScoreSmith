"use client";

import { useEffect, useRef } from "react";

import { chatPollDelayMs } from "./api-client";

/**
 * Calls `poll` repeatedly while `active` is true, using the shared `chatPollDelayMs` cadence
 * (slower while the tab is hidden, exponential backoff after failures). One poll in flight at a
 * time; stops (and aborts the in-flight poll) when `active` turns false or on unmount.
 * `slowdown` multiplies the cadence for heavier requests such as full list fetches.
 */
export function useAdaptivePoll(
  poll: (signal: AbortSignal) => Promise<void>,
  active: boolean,
  slowdown = 1,
): void {
  const pollRef = useRef(poll);
  useEffect(() => {
    pollRef.current = poll;
  });

  useEffect(() => {
    if (!active) return;
    const ac = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    const startedAt = Date.now();

    const schedule = () => {
      const delay =
        chatPollDelayMs(failures, document.visibilityState === "hidden", Date.now() - startedAt) * slowdown;
      timer = setTimeout(run, delay);
    };
    const run = async () => {
      try {
        await pollRef.current(ac.signal);
        failures = 0;
      } catch {
        if (ac.signal.aborted) return;
        failures++;
      }
      if (!ac.signal.aborted) schedule();
    };
    schedule();
    return () => {
      ac.abort();
      if (timer) clearTimeout(timer);
    };
  }, [active, slowdown]);
}
