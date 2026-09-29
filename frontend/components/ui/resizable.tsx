"use client";

/**
 * Thin design-system wrapper around `react-resizable-panels` (the current, actively
 * maintained standard for drag-to-resize layouts in React — see the research note in
 * ChatWorkspace.tsx). This project is on v4, whose primitives are named `Group` / `Panel`
 * / `Separator` — the older `PanelGroup` / `PanelResizeHandle` names that most tutorials
 * (and shadcn/ui's own `resizable.tsx` reference) still show belong to v2/v3. Re-exported
 * here under the more familiar `Resizable*` names, styled with this project's own
 * glass/hairline/focus tokens instead of introducing a new visual style.
 *
 * Accessibility comes from the library itself: the handle renders as a real
 * `role="separator"` element with `aria-orientation`, `aria-valuemin/max/now`, and full
 * keyboard support (arrow keys resize, matching WAI-ARIA separator conventions) — nothing
 * extra to wire up here beyond an `aria-label`.
 */
import type { ComponentProps } from "react";
import { GripVertical } from "lucide-react";
import { Group, Panel, Separator } from "react-resizable-panels";

import { cn } from "@/lib/utils";

function ResizablePanelGroup({ className, ...props }: ComponentProps<typeof Group>) {
  return <Group className={cn("flex h-full w-full min-h-0 min-w-0", className)} {...props} />;
}

const ResizablePanel = Panel;

/**
 * Styled for the one orientation this app actually uses: a `Group orientation="horizontal"`
 * (panels side by side), whose `Separator` renders `aria-orientation="vertical"` (per the
 * library — the ARIA attribute describes the divider's own axis, a vertical bar between
 * horizontally-arranged panels, which is the opposite of the group's orientation). If a
 * vertical group is ever added, this handle would need an `aria-orientation`-conditional
 * variant instead of these fixed classes.
 */
function ResizableHandle({
  className,
  withHandle,
  "aria-label": ariaLabel = "Resize panel",
  ...props
}: ComponentProps<typeof Separator> & { withHandle?: boolean }) {
  return (
    <Separator
      aria-label={ariaLabel}
      className={cn(
        "group relative mx-0.5 flex w-2.5 shrink-0 cursor-col-resize items-center justify-center rounded-full outline-none",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
        className,
      )}
      {...props}
    >
      <span
        aria-hidden
        className="pointer-events-none absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-hairline transition-colors group-hover:bg-lemon-ink group-data-[separator=active]:bg-lemon-ink"
      />
      {withHandle && (
        <span
          aria-hidden
          className="pointer-events-none z-10 flex h-9 w-3.5 items-center justify-center rounded-full border border-hairline bg-white/90 shadow-sm transition-colors group-hover:border-lemon-ink group-data-[separator=active]:border-lemon-ink"
        >
          <GripVertical className="size-2.5 text-ink-muted" />
        </span>
      )}
    </Separator>
  );
}

export { ResizablePanelGroup, ResizablePanel, ResizableHandle };
export type { PanelImperativeHandle } from "react-resizable-panels";
