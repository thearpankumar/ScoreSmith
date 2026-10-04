import * as React from "react";

import { cn } from "@/lib/utils";

export interface ProgressProps extends React.HTMLAttributes<HTMLDivElement> {
  /** 0..max; omit for an indeterminate (animated) bar. */
  value?: number;
  max?: number;
  label: string;
  tone?: "lemon" | "ok" | "danger";
}

/** Slim accessible progress bar (lemon fill on a hairline track). */
export function Progress({ value, max = 100, label, tone = "lemon", className, ...props }: ProgressProps) {
  const determinate = typeof value === "number";
  const pct = determinate ? Math.max(0, Math.min(100, max > 0 ? (value / max) * 100 : 0)) : 0;
  const fill = tone === "ok" ? "bg-[var(--rag-excellent)]" : tone === "danger" ? "bg-[var(--rag-poor)]" : "bg-lemon";
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={determinate ? Math.round(value) : undefined}
      className={cn("h-2 w-full overflow-hidden rounded-full bg-black/10", className)}
      {...props}
    >
      <div
        className={cn("h-full rounded-full transition-[width] duration-300", fill, !determinate && "w-1/3 animate-pulse")}
        style={determinate ? { width: `${pct}%` } : undefined}
      />
    </div>
  );
}
