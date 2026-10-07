import { Quote } from "lucide-react";

import { RagBadge } from "@/components/design-system/RagBadge";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { MarkdownContent } from "@/components/chat/MarkdownContent";
import type { EvaluationKpiResult } from "@/lib/types";

/**
 * Per-row reasoning drawer for the evaluation result tree table: shows the
 * judge's written reasoning, the evidence quotes it cited, and the exact
 * guideline text it matched against — the "basis for the rating" the
 * product scope calls for.
 */
export function ReasoningDrawer({
  result,
  open,
  onOpenChange,
  target,
}: {
  result: EvaluationKpiResult | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The scorecard target the KPI score badge is coloured against. */
  target?: number | null;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent>
        {result && (
          <>
            <SheetHeader>
              <div className="flex items-center gap-2">
                <SheetTitle>{result.kpiName}</SheetTitle>
                <RagBadge score={result.score} size="sm" target={target} />
              </div>
              <SheetDescription>Judge reasoning, evidence, and the matched guideline level.</SheetDescription>
            </SheetHeader>

            <section>
              <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-muted">Reasoning</h3>
              <MarkdownContent content={result.reasoningText} className="text-ink" />
            </section>

            <section>
              <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-muted">Evidence quoted</h3>
              <ul className="space-y-2">
                {result.evidenceQuotes.map((quote, i) => (
                  <li key={i} className="flex gap-2 rounded-lg bg-bg px-3 py-2 text-sm text-ink-muted">
                    <Quote className="mt-0.5 size-3.5 shrink-0 text-lemon-ink" aria-hidden />
                    <span className="italic">&ldquo;{quote}&rdquo;</span>
                  </li>
                ))}
              </ul>
            </section>

            <section>
              <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-muted">
                Matched guideline — level {result.matchedGuidelineLevel}
              </h3>
              <p className="rounded-lg border border-hairline bg-solid px-3 py-2 text-sm text-ink">
                {result.matchedGuidelineText || "—"}
              </p>
            </section>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
