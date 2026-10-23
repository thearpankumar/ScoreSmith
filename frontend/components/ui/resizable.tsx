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
 * Works in both group orientations. The library sets the separator's `aria-orientation` to the
 * divider's own axis, the opposite of the group's: a `Group orientation="horizontal"` (panels side
 * by side) gets a vertical bar, `aria-orientation="vertical"`; a `Group orientation="vertical"`
 * (panels stacked) gets a horizontal bar, `aria-orientation="horizontal"`. The vertical bar is the
 * default styling and the `aria-[orientation=horizontal]` variants turn it sideways.
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
        "aria-[orientation=horizontal]:mx-0 aria-[orientation=horizontal]:my-0.5 aria-[orientation=horizontal]:h-2.5 aria-[orientation=horizontal]:w-auto aria-[orientation=horizontal]:cursor-row-resize",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
        className,
      )}
      {...props}
    >
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-hairline transition-colors group-hover:bg-lemon-ink group-data-[separator=active]:bg-lemon-ink",
          "group-aria-[orientation=horizontal]:inset-x-0 group-aria-[orientation=horizontal]:inset-y-auto group-aria-[orientation=horizontal]:left-0 group-aria-[orientation=horizontal]:top-1/2 group-aria-[orientation=horizontal]:h-px group-aria-[orientation=horizontal]:w-auto group-aria-[orientation=horizontal]:translate-x-0 group-aria-[orientation=horizontal]:-translate-y-1/2",
        )}
      />
      {withHandle && (
        <span
          aria-hidden
          className="pointer-events-none z-10 flex h-9 w-3.5 items-center justify-center rounded-full border border-hairline bg-white/90 shadow-sm transition-colors group-hover:border-lemon-ink group-data-[separator=active]:border-lemon-ink group-aria-[orientation=horizontal]:h-3.5 group-aria-[orientation=horizontal]:w-9"
        >
          <GripVertical className="size-2.5 text-ink-muted group-aria-[orientation=horizontal]:rotate-90" />
        </span>
      )}
    </Separator>
  );
}

export { ResizablePanelGroup, ResizablePanel, ResizableHandle };
export type { PanelImperativeHandle } from "react-resizable-panels";
