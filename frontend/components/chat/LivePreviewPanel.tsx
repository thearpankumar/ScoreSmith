"use client";

import { useId, useState, type ReactNode } from "react";
import { ChevronDown, FileText, Plus, Send, Sparkles, Trash2, Undo2 } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { siblingWeightSum } from "@/lib/kpi-tree";
import { cn } from "@/lib/utils";
import type { DraftKpi, ScorecardDraft } from "@/lib/types";

/**
 * Live, EDITABLE preview of the in-progress scorecard draft.
 *
 * Every field and every KPI row is an inline "click-to-edit" control (a borderless
 * input that reveals its border on hover/focus) rather than static text, per the plan's
 * reference pattern that business users should be able to fix a scorecard element
 * directly instead of only through chat prose.
 *
 * Sync model (deliberately simple — see ChatWorkspace): the chat draft is
 * server-authoritative LangGraph state and the backend has no direct "patch the draft"
 * HTTP route, so edits made here are LOCAL until the next chat turn. ChatWorkspace
 * diffs this local draft against the last server draft and folds a plain-language
 * summary of the edits into the next message it sends, so the assistant applies them
 * through its own `update_draft` tool. The server's reply then becomes the new source
 * of truth. "Send edits" sends that turn immediately; "Discard" reverts to the server
 * draft.
 */
export function LivePreviewPanel({
  draft,
  onChange,
  dirty,
  onSendEdits,
  onDiscardEdits,
  disabled,
  className,
}: {
  draft: ScorecardDraft;
  onChange: (next: ScorecardDraft) => void;
  dirty: boolean;
  onSendEdits: () => void;
  onDiscardEdits: () => void;
  disabled?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(true);
  const bodyId = useId();

  const topLevel = draft.kpis.filter((k) => k.parentId === null || !draft.kpis.some((p) => p.id === k.parentId));
  // Excluded (includedInScoring=false) KPIs don't count toward or constrain the 100%
  // total — mirrors the backend trigger (migration 0005_scoring_formula_and_kpi_flags).
  const topLevelIncluded = topLevel.filter((k) => k.includedInScoring);
  const topLevelCheck = siblingWeightSum(topLevelIncluded.map((k) => k.weight));
  const topLevelSum = topLevelCheck.sum;
  const weightsOk = topLevelIncluded.length === 0 || topLevelCheck.ok;

  function patch(p: Partial<ScorecardDraft>) {
    onChange({ ...draft, ...p });
  }

  function patchKpi(id: string, p: Partial<DraftKpi>) {
    onChange({ ...draft, kpis: draft.kpis.map((k) => (k.id === id ? { ...k, ...p } : k)) });
  }

  function removeKpi(id: string) {
    // Removing a parent also removes its descendants, so no orphaned children linger.
    const doomed = new Set([id]);
    let grew = true;
    while (grew) {
      grew = false;
      for (const k of draft.kpis) {
        if (k.parentId && doomed.has(k.parentId) && !doomed.has(k.id)) {
          doomed.add(k.id);
          grew = true;
        }
      }
    }
    onChange({ ...draft, kpis: draft.kpis.filter((k) => !doomed.has(k.id)) });
  }

  function addKpi() {
    const kpi: DraftKpi = {
      id: `local-${Date.now()}`,
      name: "New KPI",
      weight: 0,
      level: 1,
      parentId: null,
      status: "proposed",
      includedInScoring: true,
    };
    onChange({ ...draft, kpis: [...draft.kpis, kpi] });
  }

  return (
    <GlassCard
      elevation={2}
      id="live-preview"
      className={cn(
        "flex min-h-0 flex-col gap-4 p-4",
        // When collapsed, don't let ambient flex-stretch from an ancestor row/panel force
        // this card to the full column height with nothing in it below the header — that
        // was the actual space-waste bug (a mostly-empty glass card, not a hidden one).
        // When open, explicitly fill the column's height (rather than relying on stretch,
        // which the resizable Panel wrapper's own layout doesn't provide) so the scroll
        // region below can bound itself and scroll internally instead of growing the page.
        open ? "h-full" : "self-start",
        className,
      )}
      aria-label="Live scorecard preview"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-ink">
          <FileText className="size-4 text-lemon-ink" aria-hidden />
          Live preview
          {dirty && <Badge variant="soft">Unsent edits</Badge>}
        </div>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="rounded-full p-1 text-ink-muted hover:bg-black/5 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
          aria-expanded={open}
          aria-controls={bodyId}
          aria-label={open ? "Collapse live preview" : "Expand live preview"}
        >
          <ChevronDown className={cn("size-4 transition-transform", !open && "-rotate-90")} />
        </button>
      </div>

      {open && (
        <div id={bodyId} className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-1 thin-scrollbar">
          <p className="text-xs text-ink-muted">
            Click any field to edit it. Edits are sent to the assistant with your next message.
          </p>

          <div>
            <InlineText
              label="Scorecard name"
              value={draft.name ?? ""}
              placeholder="Untitled scorecard"
              onChange={(v) => patch({ name: v || null })}
              disabled={disabled}
              className="text-sm font-semibold"
            />
            {draft.domain && <Badge variant="muted" className="mt-1">{draft.domain}</Badge>}
          </div>

          <InlineField label="Purpose">
            {(id) => (
              <InlineTextarea
                id={id}
                value={draft.purposeStatement ?? ""}
                placeholder="Not set yet — click to add"
                onChange={(v) => patch({ purposeStatement: v || null })}
                disabled={disabled}
              />
            )}
          </InlineField>

          <InlineField label="Scope / audience">
            {(id) => (
              <InlineTextarea
                id={id}
                value={draft.scope ?? ""}
                placeholder="Not set yet — click to add"
                onChange={(v) => patch({ scope: v || null })}
                disabled={disabled}
              />
            )}
          </InlineField>

          <InlineField label="Target score">
            {(id) => (
              <div className="flex items-center gap-1.5">
                <input
                  id={id}
                  type="number"
                  min={0}
                  max={10}
                  step={0.5}
                  inputMode="decimal"
                  value={draft.targetScore ?? ""}
                  placeholder="—"
                  disabled={disabled}
                  onChange={(e) => {
                    const raw = e.target.value;
                    if (raw === "") return patch({ targetScore: null });
                    const n = Math.min(10, Math.max(0, Number(raw)));
                    if (Number.isFinite(n)) patch({ targetScore: n });
                  }}
                  className={cn(INLINE_INPUT, "w-20 text-right tabular-nums")}
                />
                <span className="text-sm text-ink-muted">/ 10</span>
              </div>
            )}
          </InlineField>

          <div>
            <div className="mb-2 flex items-center justify-between gap-2">
              <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
                KPIs {draft.kpis.length > 0 && <span className="normal-case">({draft.kpis.length})</span>}
              </p>
              {topLevel.length > 0 && (
                <span
                  className={cn(
                    "text-xs font-medium tabular-nums",
                    weightsOk ? "text-[var(--rag-excellent)]" : "text-[var(--rag-poor)]",
                  )}
                  title="Top-level KPI weights should sum to 100%"
                >
                  Σ {topLevelSum}%
                </span>
              )}
            </div>

            {draft.kpis.length === 0 && <p className="mb-2 text-xs italic text-ink-muted">Not proposed yet.</p>}

            <ul className="space-y-1.5">
              {draft.kpis.map((kpi) => (
                <li
                  key={kpi.id}
                  className={cn(
                    "flex items-center gap-1.5 rounded-lg border px-1.5 py-1",
                    kpi.status === "proposed" ? "border-dashed border-hairline" : "border-hairline bg-white/70",
                  )}
                  style={{ marginLeft: `${(kpi.level - 1) * 0.75}rem` }}
                >
                  <div className="min-w-0 flex-1">
                    <input
                      type="text"
                      value={kpi.name}
                      aria-label={`KPI name (level ${kpi.level})`}
                      disabled={disabled}
                      onChange={(e) => patchKpi(kpi.id, { name: e.target.value })}
                      className={cn(INLINE_INPUT, "w-full text-xs")}
                    />
                    {kpi.status === "proposed" && (
                      <span className="ml-1.5 flex items-center gap-0.5 text-[10px] text-lemon-ink">
                        <Sparkles className="size-2.5" aria-hidden />
                        proposed
                      </span>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center">
                    <input
                      type="number"
                      min={0}
                      max={100}
                      step={1}
                      inputMode="numeric"
                      value={Number.isFinite(kpi.weight) ? kpi.weight : ""}
                      aria-label={`Weight for ${kpi.name || "KPI"} (percent)`}
                      disabled={disabled}
                      onChange={(e) => {
                        const n = e.target.value === "" ? 0 : Math.min(100, Math.max(0, Number(e.target.value)));
                        if (Number.isFinite(n)) patchKpi(kpi.id, { weight: n });
                      }}
                      className={cn(INLINE_INPUT, "w-14 text-right text-xs tabular-nums")}
                    />
                    <span className="text-xs text-ink-muted">%</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => patchKpi(kpi.id, { includedInScoring: !kpi.includedInScoring })}
                    disabled={disabled}
                    title={
                      kpi.includedInScoring
                        ? "Exclude from the weighted score (still tracked/scored, just doesn't count toward the 100% total)"
                        : "Include back in the weighted score"
                    }
                    className={cn(
                      "shrink-0 rounded px-1 py-0.5 text-[9px] font-medium uppercase tracking-wide",
                      "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]",
                      kpi.includedInScoring
                        ? "text-ink-muted hover:bg-black/5 hover:text-ink"
                        : "bg-lemon-soft/60 text-lemon-ink",
                    )}
                  >
                    {kpi.includedInScoring ? "Scored" : "Excl."}
                  </button>
                  <button
                    type="button"
                    onClick={() => removeKpi(kpi.id)}
                    disabled={disabled}
                    className="shrink-0 rounded-md p-1 text-ink-muted hover:bg-black/5 hover:text-[var(--rag-poor)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)] disabled:opacity-50"
                    aria-label={`Remove KPI ${kpi.name}`}
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                </li>
              ))}
            </ul>

            <Button type="button" variant="ghost" size="sm" onClick={addKpi} disabled={disabled} className="mt-2">
              <Plus className="size-3.5" aria-hidden />
              Add KPI
            </Button>
          </div>

          {dirty && (
            <div className="flex flex-wrap gap-2 border-t border-hairline pt-3">
              <Button type="button" size="sm" onClick={onSendEdits} disabled={disabled}>
                <Send className="size-3.5" aria-hidden />
                Send edits to assistant
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={onDiscardEdits} disabled={disabled}>
                <Undo2 className="size-3.5" aria-hidden />
                Discard
              </Button>
            </div>
          )}
        </div>
      )}
    </GlassCard>
  );
}

const INLINE_INPUT =
  "rounded-md border border-transparent bg-transparent px-1.5 py-1 text-ink placeholder:italic placeholder:text-ink-muted transition-colors hover:border-hairline hover:bg-white/60 focus:border-hairline focus:bg-white focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)] disabled:cursor-not-allowed disabled:opacity-60";

function InlineField({ label, children }: { label: string; children: (id: string) => ReactNode }) {
  const id = useId();
  return (
    <div>
      <label htmlFor={id} className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
        {label}
      </label>
      <div className="mt-0.5">{children(id)}</div>
    </div>
  );
}

function InlineText({
  label,
  value,
  placeholder,
  onChange,
  disabled,
  className,
}: {
  label: string;
  value: string;
  placeholder: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <input
      type="text"
      aria-label={label}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className={cn(INLINE_INPUT, "w-full", className)}
    />
  );
}

function InlineTextarea({
  id,
  value,
  placeholder,
  onChange,
  disabled,
}: {
  id: string;
  value: string;
  placeholder: string;
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  return (
    <textarea
      id={id}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      rows={value.length > 80 ? 3 : 2}
      onChange={(e) => onChange(e.target.value)}
      className={cn(INLINE_INPUT, "w-full resize-y text-sm leading-relaxed")}
    />
  );
}
