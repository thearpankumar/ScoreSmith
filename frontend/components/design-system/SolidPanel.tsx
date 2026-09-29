import * as React from "react";

import { cn } from "@/lib/utils";

export interface SolidPanelProps extends React.HTMLAttributes<HTMLDivElement> {
  as?: keyof React.JSX.IntrinsicElements;
}

/**
 * Solid `#FFFFFF` surface, mandatory for dense data per the design system:
 * the guideline matrix, KPI/weight tables, evaluation tables, and forms all
 * use this instead of glass — transparency over dense grids of numbers
 * hurts legibility, so glass is chrome/cards/hero only.
 */
export function SolidPanel({ as = "div", className, children, ...props }: SolidPanelProps) {
  const Comp = as as React.ElementType;
  return (
    <Comp className={cn("solid-panel rounded-2xl", className)} {...props}>
      {children}
    </Comp>
  );
}
