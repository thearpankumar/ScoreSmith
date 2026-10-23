"use client";

import { useRef } from "react";

import { Dialog, DialogContent } from "@/components/ui/dialog";
import type { FormulaValidation } from "@/lib/api-client";
import type { FormulaKpi } from "@/lib/formula-kpis";
import { usePersistedState } from "@/lib/usePersistedState";
import { cn } from "@/lib/utils";
import { FormulaBuilder } from "./formula-builder/FormulaBuilder";

/**
 * The visual scoring-formula builder in a large dialog (see formula-builder/FormulaBuilder for the
 * builder itself). Sized to the viewport rather than to its content, so a scorecard with a hundred long
 * KPI names scrolls inside the KPI pane instead of pushing the dialog off screen; on desktop it can be
 * maximized (remembered per browser).
 *
 * Live validation calls `onValidate`, the SAME backend validation the plain-text editor
 * (ScoringFormulaPanel) uses, injected by the caller (a saved scorecard's version, or an in-progress chat
 * draft), so the two surfaces can never disagree about what's valid. `onApply` receives the formula text,
 * or `initialText` untouched when nothing was changed.
 */
export function ScoringFormulaBuilderDialog({
  open,
  onOpenChange,
  initialText,
  kpis,
  onValidate,
  onApply,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialText: string;
  /** The scorecard's leaf KPIs, in scorecard order (see `formulaKpis`). */
  kpis: FormulaKpi[];
  /** Real backend validation — see ScoringFormulaPanel's own `onValidate` prop. */
  onValidate: (formula: string) => Promise<FormulaValidation>;
  onApply: (text: string) => void;
}) {
  const [maximized, setMaximized] = usePersistedState("qs.formulaBuilder.maximized", false);
  const escapeGuard = useRef<() => boolean>(() => false);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn(
          "flex max-w-none flex-col gap-0 overflow-hidden p-0",
          "h-[calc(100dvh-1rem)] w-[calc(100vw-1rem)] rounded-xl sm:rounded-2xl",
          !maximized && "md:h-[min(54rem,calc(100dvh-3rem))] md:w-[min(80rem,calc(100vw-3rem))]",
        )}
        onOpenAutoFocus={(e) => e.preventDefault()} // the builder focuses its search box / formula itself
        onEscapeKeyDown={(e) => {
          if (escapeGuard.current()) e.preventDefault();
        }}
        // A stray click on the backdrop shouldn't throw away a half-built formula: close with
        // Cancel, the X or Escape.
        onInteractOutside={(e) => e.preventDefault()}
      >
        <FormulaBuilder
          initialText={initialText}
          kpis={kpis}
          onValidate={onValidate}
          onApply={onApply}
          onCancel={() => onOpenChange(false)}
          maximized={maximized}
          onToggleMaximized={() => setMaximized(!maximized)}
          escapeGuard={escapeGuard}
        />
      </DialogContent>
    </Dialog>
  );
}
