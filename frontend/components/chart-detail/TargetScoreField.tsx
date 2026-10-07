"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Check, Loader2, Pencil, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ApiError, updateScorecardTarget } from "@/lib/api-client";
import { describeTargetThresholds } from "@/lib/rag";
import { parseTargetInput } from "@/lib/target-score";

/**
 * Inline editor for a scorecard's target score. Every result — badges, bullet chart, the
 * Evaluations list, the Excel export — is coloured against this number, so the hint line
 * under it spells out where each band starts for the current value.
 */
export function TargetScoreField({
  scorecardId,
  target,
  canEdit,
  lockedReason,
}: {
  scorecardId: string;
  /** Stored target; 0 / null mean "none set" (the app default of 7 is used for colours). */
  target: number | null;
  canEdit: boolean;
  lockedReason?: string;
}) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const hasTarget = typeof target === "number" && target > 0;

  function startEditing() {
    setDraft(hasTarget ? String(target) : "7");
    setError(null);
    setEditing(true);
    requestAnimationFrame(() => inputRef.current?.select());
  }

  function cancel() {
    if (saving) return;
    setEditing(false);
    setError(null);
  }

  async function save() {
    const parsed = parseTargetInput(draft);
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await updateScorecardTarget(scorecardId, parsed.value);
      setEditing(false);
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : "Could not save the target.");
    } finally {
      setSaving(false);
    }
  }

  const errorId = `target-error-${scorecardId}`;

  return (
    <div className="flex flex-col items-end gap-1 text-right">
      {editing ? (
        <div className="flex items-center gap-1.5">
          <label className="sr-only" htmlFor={`target-${scorecardId}`}>
            Target score (0.1 to 10)
          </label>
          <input
            id={`target-${scorecardId}`}
            ref={inputRef}
            type="number"
            inputMode="decimal"
            step={0.1}
            min={0.1}
            max={10}
            value={draft}
            disabled={saving}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void save();
              } else if (e.key === "Escape") {
                e.preventDefault();
                cancel();
              }
            }}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errorId : undefined}
            className="h-8 w-20 rounded-lg border border-hairline bg-solid px-2 text-right text-sm tabular-nums text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)] disabled:opacity-60"
          />
          <span className="text-xs text-ink-muted">/ 10</span>
          <Button size="sm" onClick={() => void save()} disabled={saving} aria-label="Save target score">
            {saving ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : <Check className="size-3.5" aria-hidden />}
            Save
          </Button>
          <Button size="sm" variant="ghost" onClick={cancel} disabled={saving} aria-label="Cancel editing target score">
            <X className="size-3.5" aria-hidden />
          </Button>
        </div>
      ) : (
        <div className="flex items-center gap-1.5">
          <span className="font-medium tabular-nums text-ink">{hasTarget ? `${target.toFixed(1)} / 10` : "Not set (7.0 used)"}</span>
          {canEdit ? (
            <Button size="sm" variant="ghost" onClick={startEditing} aria-label="Edit target score">
              <Pencil className="size-3.5" aria-hidden />
            </Button>
          ) : null}
        </div>
      )}
      {error && (
        <p id={errorId} role="alert" className="max-w-[16rem] text-xs text-[var(--rag-poor)]">
          {error}
        </p>
      )}
      <p className="max-w-[16rem] text-[11px] leading-snug text-ink-muted">
        Colours follow the target: {describeTargetThresholds(target)}
      </p>
      {!canEdit && lockedReason && <p className="max-w-[16rem] text-[11px] leading-snug text-ink-muted">{lockedReason}</p>}
    </div>
  );
}
