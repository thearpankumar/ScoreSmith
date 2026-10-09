"use client";

import { useEffect, useLayoutEffect, useRef, type ReactNode } from "react";

/** The design's reference canvas. */
export const STAGE_W = 1672;
export const STAGE_H = 941;
/** Below this viewport width the page switches from the scaled canvas to the stacked, fluid layout. */
export const STACK_BELOW = 1100;

// useLayoutEffect warns during SSR; this alias runs the same code after hydration on the server render path.
const useIsoLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

/**
 * Scales the fixed 1672x941 sign-in canvas to the viewport ("contain", never distorting) by publishing
 * `--stage-scale` on the root element, which the stylesheet turns into a transform. The content therefore matches
 * the design exactly at 1672x941 and keeps its proportions at every other size >= 1100px wide. Below that the
 * stylesheet drops the canvas and stacks the content instead, so no scale is needed.
 *
 * `data-ready` flips on once the scale is known (hydration), so the unscaled canvas is never painted.
 */
export function ScaledStage({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);

  useIsoLayoutEffect(() => {
    const root = ref.current;
    if (!root) return;
    const apply = () => {
      const s = Math.min(window.innerWidth / STAGE_W, window.innerHeight / STAGE_H);
      root.style.setProperty("--stage-scale", String(s));
      root.dataset.ready = "true";
    };
    apply();
    window.addEventListener("resize", apply);
    return () => window.removeEventListener("resize", apply);
  }, []);

  return (
    <div ref={ref} className="auth-root">
      {children}
    </div>
  );
}
