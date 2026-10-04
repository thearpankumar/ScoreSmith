// Plain (non-"use client") module so server components can read these values; anything
// exported from a "use client" file reaches the server as an opaque client reference.
export const CHART_TABS = ["overview", "guidelines", "evaluate", "history"] as const;
export type ChartTab = (typeof CHART_TABS)[number];
