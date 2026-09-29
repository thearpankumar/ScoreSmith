import * as React from "react";

import { cn } from "@/lib/utils";

export interface GlassCardProps extends React.HTMLAttributes<HTMLDivElement> {
  /** 1 = subtlest (nav chrome), 2 = default (cards), 3 = strongest (modals / hero). */
  elevation?: 1 | 2 | 3;
  as?: keyof React.JSX.IntrinsicElements;
}

/**
 * Glass surface primitive. Use for chrome, cards, modals, and hero
 * elements — never for dense data (guideline matrix, KPI/weight tables,
 * evaluation tables, forms), which must use `SolidPanel` instead per the
 * design system's glass-vs-solid rule.
 */
export function GlassCard({ elevation = 2, as = "div", className, children, ...props }: GlassCardProps) {
  const Comp = as as React.ElementType;
  return (
    <Comp
      className={cn(`glass-elev-${elevation}`, "rounded-2xl", className)}
      {...props}
    >
      {children}
    </Comp>
  );
}
