"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, CheckCircle2, LayoutGrid, Loader2, Sigma, X } from "lucide-react";

import { SolidPanel } from "@/components/design-system/SolidPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError, updateScoringFormula, validateScoringFormula, type FormulaValidation } from "@/lib/api-client";
import { cn } from "@/lib/utils";
import { ScoringFormulaBuilderDialog } from "./ScoringFormulaBuilderDialog";

const VALIDATE_DEBOUNCE_MS = 400;

/**
 * Part 2b — custom scoring formula editor (required piece; see
 * backend/app/ai/scoring_formula.py for the shared safe-expression evaluator this talks
 * to). Placed on the Overview tab, alongside the KPI structure tree, since a formula only
 * makes sense in the context of the KPI names it references.
 *
 * `NULL` (shown as an empty editor with the "using the default weighted average" note) is
 * the default for every scorecard unless explicitly customized — clearing the text and
 * saving reverts to that, byte-for-byte unchanged default behavior.
 *
 * Live validation calls the REAL backend endpoint (`POST .../validate-formula`) on every
 * keystroke (debounced), rather than re-implementing formula parsing client-side, so this
 * can never drift from what actually gets accepted/evaluated (the same rule "Save" itself
 * is checked against server-side).
 */
export function ScoringFormulaPanel({
  scorecardId,
  versionId,
  initialFormula,
  leafKpiNames,
}: {
  scorecardId: string;
  versionId: string;
  initialFormula: string | null;
  leafKpiNames: string[];
}) {
  const router = useRouter();
  const [text, setText] = useState(initialFormula ?? "");
  const [savedFormula, setSavedFormula] = useState(initialFormula);
  const [validation, setValidation] = useState<FormulaValidation | null>(null);
  const [validating, setValidating] = useState(false);
  const [validateError, setValidateError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveNotice, setSaveNotice] = useState<string | null>(null);
  const [dismissedUnused, setDismissedUnused] = useState(false);
  const [builderOpen, setBuilderOpen] = useState(false);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestIdRef = useRef(0);

  const isDirty = text.trim() !== (savedFormula ?? "").trim();
  const isBlank = text.trim() === "";

  useEffect(() => {
    setDismissedUnused(false);
    if (debounceRef.current) clearTimeout(debounceRef.current);

    if (isBlank) {
      // Blank == "use the default weighted average" — always valid, nothing to check.
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
        const result = await validateScoringFormula(scorecardId, versionId, text);
        if (requestIdRef.current !== myRequestId) return; // a newer keystroke superseded this
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- scorecardId/versionId are stable per mount
  }, [text]);

  async function save() {
    if (validating || (validation && !validation.valid)) return;
    setSaving(true);
    setSaveError(null);
    setSaveNotice(null);
    const next = isBlank ? null : text.trim();
    try {
      await updateScoringFormula(scorecardId, versionId, next);
      setSavedFormula(next);
      setSaveNotice(next ? "Custom formula saved." : "Reverted to the default weighted average.");
      router.refresh();
    } catch (err) {
      setSaveError(
        err instanceof ApiError ? err.message : err instanceof Error ? err.message : "Could not save the formula.",
      );
    } finally {
      setSaving(false);
    }
  }

  function revertToDefault() {
    setText("");
  }

  const showValid = !isBlank && !validating && validation?.valid && !validateError;
  const showInvalid = !isBlank && !validating && validation && !validation.valid;
  const unusedKpis = validation?.unusedKpis ?? [];

  return (
    <SolidPanel className="flex flex-col gap-3 p-5">
      <div className="flex items-center justify-between gap-2">
        <p className="flex items-center gap-2 text-sm font-semibold text-ink">
          <Sigma className="size-4 text-lemon-ink" aria-hidden />
          Scoring formula
        </p>
        {savedFormula ? <Badge variant="lemon">Custom</Badge> : <Badge variant="muted">Default weighted average</Badge>}
      </div>

      <p className="text-xs leading-relaxed text-ink-muted">
        By default the final score is Σ(weight × score) / 100. Reference a KPI&apos;s score as{" "}
        <code className="rounded bg-black/5 px-1 py-0.5 font-mono text-[11px]">kpi[&quot;KPI Name&quot;]</code> to
        write your own formula instead — supports <code className="font-mono text-[11px]">+ − × ÷ **</code> and{" "}
        <code className="font-mono text-[11px]">min, max, avg, sqrt, abs</code>. Leave blank to use the default.
      </p>

      <Button type="button" size="sm" variant="outline" onClick={() => setBuilderOpen(true)} className="self-start">
        <LayoutGrid className="size-3.5" aria-hidden />
        Open formula builder
      </Button>

      <div className="relative">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder='e.g. min(kpi["Compliance"], kpi["Security Review"]) * 0.6 + kpi["Docs Quality"] * 0.4'
          rows={3}
          spellCheck={false}
          className={cn(
            "w-full resize-y rounded-lg border bg-bg/60 px-3 py-2 font-mono text-xs leading-relaxed text-ink",
            "placeholder:font-sans placeholder:italic placeholder:text-ink-muted",
            "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]",
            showValid && "border-[var(--rag-excellent)] bg-[var(--rag-excellent)]/5",
            showInvalid && "border-[var(--rag-poor)] bg-[var(--rag-poor)]/5",
            !showValid && !showInvalid && "border-hairline",
          )}
          aria-invalid={!!showInvalid}
          aria-describedby="scoring-formula-status"
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

      <div id="scoring-formula-status" aria-live="polite" className="min-h-4 text-xs">
        {isBlank ? (
          <span className="text-ink-muted">Blank — will use the default weighted average.</span>
        ) : validating ? (
          <span className="text-ink-muted">Validating…</span>
        ) : validateError ? (
          <span className="text-[var(--rag-poor)]">{validateError}</span>
        ) : showInvalid ? (
          <span className="text-[var(--rag-poor)]">{validation!.error}</span>
        ) : showValid ? (
          <span className="text-[var(--rag-excellent)]">Valid — ready to save.</span>
        ) : null}
      </div>

      {showValid && unusedKpis.length > 0 && !dismissedUnused && (
        <div className="flex items-start gap-2 rounded-lg border border-hairline bg-lemon-soft/40 px-3 py-2 text-xs text-ink">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-lemon-ink" aria-hidden />
          <div className="flex-1">
            <p className="font-medium">
              This formula never references: {unusedKpis.map((n) => `"${n}"`).join(", ")}.
            </p>
            <p className="mt-0.5 text-ink-muted">Those KPIs are still tracked/scored, just not used by this formula.</p>
          </div>
          <button
            type="button"
            onClick={() => setDismissedUnused(true)}
            className="shrink-0 rounded p-0.5 text-ink-muted hover:bg-black/5 hover:text-ink"
            aria-label="Dismiss this warning — I did this on purpose"
            title="I did this on purpose"
          >
            <X className="size-3.5" />
          </button>
        </div>
      )}

      {leafKpiNames.length > 0 && (
        <details className="text-xs text-ink-muted">
          <summary className="cursor-pointer select-none font-medium hover:text-ink">
            Available KPI names ({leafKpiNames.length})
          </summary>
          <ul className="mt-1.5 flex flex-wrap gap-1">
            {leafKpiNames.map((name) => (
              <li key={name}>
                <code className="rounded bg-black/5 px-1 py-0.5 font-mono text-[11px]">kpi[&quot;{name}&quot;]</code>
              </li>
            ))}
          </ul>
        </details>
      )}

      {saveError && (
        <p className="text-xs text-[var(--rag-poor)]" role="alert">
          {saveError}
        </p>
      )}
      {saveNotice && !isDirty && <p className="text-xs text-[var(--rag-excellent)]">{saveNotice}</p>}

      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          onClick={save}
          disabled={!isDirty || saving || validating || (!isBlank && validation ? !validation.valid : false)}
        >
          {saving ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : null}
          {isBlank ? "Revert to default" : "Save formula"}
        </Button>
        {isDirty && (
          <Button type="button" size="sm" variant="ghost" onClick={() => setText(savedFormula ?? "")} disabled={saving}>
            Cancel
          </Button>
        )}
        {!isDirty && savedFormula && (
          <Button type="button" size="sm" variant="ghost" onClick={revertToDefault} disabled={saving}>
            Clear custom formula
          </Button>
        )}
      </div>

      <ScoringFormulaBuilderDialog
        open={builderOpen}
        onOpenChange={setBuilderOpen}
        scorecardId={scorecardId}
        versionId={versionId}
        initialText={text}
        leafKpiNames={leafKpiNames}
        onApply={(next) => {
          setText(next);
          setBuilderOpen(false);
        }}
      />
    </SolidPanel>
  );
}
