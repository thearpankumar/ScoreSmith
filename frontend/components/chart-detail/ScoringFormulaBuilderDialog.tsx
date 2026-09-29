"use client";

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { FormulaValidation } from "@/lib/api-client";
import { cn } from "@/lib/utils";

const VALIDATE_DEBOUNCE_MS = 350;

const OPERATORS = [
  { label: "+", insert: " + " },
  { label: "−", insert: " - " },
  { label: "×", insert: " * " },
  { label: "÷", insert: " / " },
  { label: "^", insert: "**", title: "Exponent (raise to power)" },
  { label: "( )", insert: "()", cursorOffset: -1, title: "Parentheses" },
];

const FUNCTIONS = [
  { label: "min", insert: "min(, )", cursorOffset: -3, title: "Smallest of the values" },
  { label: "max", insert: "max(, )", cursorOffset: -3, title: "Largest of the values" },
  { label: "avg", insert: "avg(, )", cursorOffset: -3, title: "Average of the values" },
  { label: "sqrt", insert: "sqrt()", cursorOffset: -1, title: "Square root" },
  { label: "abs", insert: "abs()", cursorOffset: -1, title: "Absolute value" },
];

/**
 * Visual formula-builder modal (Part 2b's "attempt if time allows" piece) — a palette of
 * insertable tokens (KPI references, operators, functions) next to a live-validated
 * expression field, per the owner's ask for something that doesn't require typing symbols
 * that are awkward on a keyboard. Deliberately a plain button-grid + text-insertion-at-
 * cursor widget rather than a drag-and-drop visual expression tree — a quick websearch
 * (see this pass's task notes) turned up no React formula-builder library that's a better
 * fit for "type-safe insertion into a text expression" than hand-rolling this, and the
 * actual need (per the task) doesn't call for more sophistication than this.
 *
 * Uses the shadcn `Dialog` primitive + design tokens, consistent with every other modal in
 * this app (see ScorecardActions.tsx's confirm dialogs). Live validation here calls
 * `onValidate` — the SAME real backend validation the plain-text editor (ScoringFormulaPanel)
 * uses, injected by the caller (a saved scorecard's version, or an in-progress chat draft —
 * see Issue 2's task notes), so the two surfaces can never disagree about what's valid.
 */
export function ScoringFormulaBuilderDialog({
  open,
  onOpenChange,
  initialText,
  leafKpiNames,
  onValidate,
  onApply,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialText: string;
  leafKpiNames: string[];
  /** Real backend validation — see ScoringFormulaPanel's own `onValidate` prop. */
  onValidate: (formula: string) => Promise<FormulaValidation>;
  onApply: (text: string) => void;
}) {
  const [text, setText] = useState(initialText);
  const [validation, setValidation] = useState<FormulaValidation | null>(null);
  const [validating, setValidating] = useState(false);
  const [validateError, setValidateError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestIdRef = useRef(0);

  // Reset to the panel's current text every time the modal is (re)opened.
  useEffect(() => {
    if (open) setText(initialText);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-sync on open, not on every initialText identity change
  }, [open]);

  useEffect(() => {
    if (!open) return;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const trimmed = text.trim();
    if (!trimmed) {
      setValidation({ valid: true, error: null, unusedKpis: [] });
      setValidating(false);
      setValidateError(null);
      return;
    }
    setValidating(true);
    setValidateError(null);
    const myRequestId = ++requestIdRef.current;
    debounceRef.current = setTimeout(async () => {
      try {
        const result = await onValidate(text);
        if (requestIdRef.current !== myRequestId) return;
        setValidation(result);
      } catch (err) {
        if (requestIdRef.current !== myRequestId) return;
        setValidateError(err instanceof Error ? err.message : "Could not validate right now.");
        setValidation(null);
      } finally {
        if (requestIdRef.current === myRequestId) setValidating(false);
      }
    }, VALIDATE_DEBOUNCE_MS);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onValidate is stable per mount (bound at call site)
  }, [text, open]);

  /** Inserts `token` at the current cursor position (replacing any selection), then
   * restores focus with the caret placed `cursorOffset` characters back from the end of
   * the inserted token (e.g. so inserting `min(, )` lands the caret between the parens'
   * first argument slot, not after the whole token). */
  function insertToken(token: string, cursorOffset = 0) {
    const el = textareaRef.current;
    if (!el) {
      setText((t) => t + token);
      return;
    }
    const start = el.selectionStart ?? text.length;
    const end = el.selectionEnd ?? text.length;
    const next = text.slice(0, start) + token + text.slice(end);
    setText(next);
    const caret = start + token.length + cursorOffset;
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(caret, caret);
    });
  }

  const isBlank = text.trim() === "";
  const showValid = !isBlank && !validating && validation?.valid && !validateError;
  const showInvalid = !isBlank && !validating && validation && !validation.valid;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Build a scoring formula</DialogTitle>
          <DialogDescription>
            Click a KPI, operator, or function to insert it where your cursor is — no need to type symbols by hand.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div>
            <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-muted">KPIs</p>
            <div className="flex flex-wrap gap-1.5">
              {leafKpiNames.length === 0 && <p className="text-xs italic text-ink-muted">No scored KPIs yet.</p>}
              {leafKpiNames.map((name) => (
                <button
                  key={name}
                  type="button"
                  onClick={() => insertToken(`kpi["${name}"]`)}
                  className="rounded-full border border-hairline bg-solid px-2.5 py-1 text-xs font-medium text-ink hover:border-lemon-ink hover:bg-lemon-soft/60 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]"
                  title={`Insert kpi["${name}"]`}
                >
                  {name}
                </button>
              ))}
            </div>
          </div>

          <div className="flex flex-wrap gap-4">
            <div>
              <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-muted">Operators</p>
              <div className="flex flex-wrap gap-1.5">
                {OPERATORS.map((op) => (
                  <button
                    key={op.label}
                    type="button"
                    onClick={() => insertToken(op.insert, op.cursorOffset ?? 0)}
                    title={op.title ?? op.label}
                    className="flex size-8 items-center justify-center rounded-md border border-hairline bg-solid text-sm font-semibold text-ink hover:border-lemon-ink hover:bg-lemon-soft/60 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]"
                  >
                    {op.label}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-muted">Functions</p>
              <div className="flex flex-wrap gap-1.5">
                {FUNCTIONS.map((fn) => (
                  <button
                    key={fn.label}
                    type="button"
                    onClick={() => insertToken(fn.insert, fn.cursorOffset ?? 0)}
                    title={fn.title}
                    className="rounded-md border border-hairline bg-solid px-2 py-1 font-mono text-xs font-medium text-ink hover:border-lemon-ink hover:bg-lemon-soft/60 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]"
                  >
                    {fn.label}()
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="relative">
            <textarea
              ref={textareaRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              spellCheck={false}
              placeholder='e.g. min(kpi["Speed"], kpi["Accuracy"]) * 0.7 + kpi["Accuracy"] * 0.3'
              className={cn(
                "w-full resize-y rounded-lg border bg-bg/60 px-3 py-2 font-mono text-xs leading-relaxed text-ink",
                "placeholder:font-sans placeholder:italic placeholder:text-ink-muted",
                "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]",
                showValid && "border-[var(--rag-excellent)] bg-[var(--rag-excellent)]/5",
                showInvalid && "border-[var(--rag-poor)] bg-[var(--rag-poor)]/5",
                !showValid && !showInvalid && "border-hairline",
              )}
            />
            <div className="pointer-events-none absolute right-2 top-2">
              {validating ? (
                <Loader2 className="size-4 animate-spin text-ink-muted" aria-hidden />
              ) : showValid ? (
                <CheckCircle2 className="size-4 text-[var(--rag-excellent)]" aria-hidden />
              ) : showInvalid ? (
                <AlertTriangle className="size-4 text-[var(--rag-poor)]" aria-hidden />
              ) : null}
            </div>
          </div>

          <div aria-live="polite" className="min-h-4 text-xs">
            {isBlank ? (
              <span className="text-ink-muted">Blank — will use the default weighted average.</span>
            ) : validating ? (
              <span className="text-ink-muted">Validating…</span>
            ) : validateError ? (
              <span className="text-[var(--rag-poor)]">{validateError}</span>
            ) : showInvalid ? (
              <span className="text-[var(--rag-poor)]">{validation!.error}</span>
            ) : showValid ? (
              <span className="text-[var(--rag-excellent)]">Valid.</span>
            ) : null}
          </div>
        </div>

        <DialogFooter>
          <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            onClick={() => onApply(text)}
            disabled={validating || (!isBlank && validation ? !validation.valid : false)}
          >
            Use this formula
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
