"use client";

import { useState } from "react";
import { AlertTriangle, Check, CheckCircle2, Copy, Loader2 } from "lucide-react";

import { spaceBefore, tokenLabel, type FormulaToken } from "@/lib/formula-tokens";
import { cn } from "@/lib/utils";
import { DEPTH_TINTS, NUMBER_COLOR } from "./FormulaCanvas";

export type CheckState =
  | { kind: "blank" }
  | { kind: "checking" }
  | { kind: "valid" }
  | { kind: "invalid"; message: string }
  | { kind: "unavailable"; message: string };

/**
 * The formula read back in words (KPI names, × ÷ −), the backend's verdict on it, and the exact text that
 * will be saved. `tokens` is null when the text can't be shown as tokens (text mode with unsupported
 * syntax); the text is then shown as is.
 */
export function FormulaPreview({
  tokens,
  text,
  check,
}: {
  tokens: FormulaToken[] | null;
  text: string;
  check: CheckState;
}) {
  const [copied, setCopied] = useState(false);
  const blank = text.trim() === "";

  async function copy() {
    try {
      await navigator.clipboard.writeText(text.trim());
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard blocked (permissions / insecure context): the text is selectable below anyway.
    }
  }

  return (
    <section aria-label="Formula preview" className="shrink-0 rounded-xl border border-hairline bg-bg/70 p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-semibold text-ink">Formula preview</p>
        <CheckBadge check={check} />
      </div>
      <p className="thin-scrollbar mt-2 max-h-32 overflow-y-auto break-words rounded-lg border border-hairline bg-solid px-3 py-2 font-mono text-[13px] leading-relaxed text-ink">
        {blank ? (
          <span className="font-sans text-ink-muted">Default weighted average: Σ(weight × score) / 100</span>
        ) : tokens ? (
          tokens.map((t, i) => (
            <span key={t.id}>
              {spaceBefore(tokens, i) ? " " : ""}
              <span
                className={cn(
                  t.kind === "kpi" && "font-sans font-medium",
                  (t.kind === "op" || t.kind === "comma") && "text-ink-muted",
                )}
                style={
                  t.kind === "number"
                    ? { color: NUMBER_COLOR }
                    : t.kind === "fn"
                      ? { color: DEPTH_TINTS[0].fg }
                      : undefined
                }
              >
                {tokenLabel(t)}
              </span>
            </span>
          ))
        ) : (
          text
        )}
      </p>
      <p aria-live="polite" className="mt-2 min-h-4 text-xs">
        {check.kind === "blank" ? (
          <span className="text-ink-muted">Blank: the default weighted average is used.</span>
        ) : check.kind === "checking" ? (
          <span className="text-ink-muted">Checking…</span>
        ) : check.kind === "valid" ? (
          <span className="text-[var(--rag-excellent)]">Valid formula.</span>
        ) : (
          <span className="text-[var(--rag-poor)]">{check.message}</span>
        )}
      </p>
      {!blank && (
        <details className="mt-1 text-xs text-ink-muted">
          <summary className="cursor-pointer select-none font-medium hover:text-ink">Saved as</summary>
          <div className="mt-1.5 flex items-start gap-2">
            <code className="thin-scrollbar block max-h-24 flex-1 overflow-y-auto break-all rounded-md bg-black/[0.04] px-2 py-1.5 font-mono text-[11px] text-ink">
              {text.trim()}
            </code>
            <button
              type="button"
              onClick={copy}
              aria-label={copied ? "Copied" : "Copy the formula text"}
              title="Copy"
              className="shrink-0 rounded-md p-1.5 text-ink-muted hover:bg-black/5 hover:text-ink"
            >
              {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
            </button>
          </div>
        </details>
      )}
    </section>
  );
}

function CheckBadge({ check }: { check: CheckState }) {
  if (check.kind === "checking") return <Loader2 className="size-4 animate-spin text-ink-muted" aria-hidden />;
  if (check.kind === "valid") return <CheckCircle2 className="size-4 text-[var(--rag-excellent)]" aria-hidden />;
  if (check.kind === "invalid" || check.kind === "unavailable") {
    return <AlertTriangle className="size-4 text-[var(--rag-poor)]" aria-hidden />;
  }
  return null;
}
