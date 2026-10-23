"use client";

import {
  Fragment,
  memo,
  useEffect,
  useMemo,
  useState,
  type KeyboardEvent,
  type MouseEvent,
  type RefObject,
} from "react";
import { useDndContext, useDraggable, useDroppable } from "@dnd-kit/core";
import { X } from "lucide-react";

import { kpiTint, type FormulaKpi } from "@/lib/formula-kpis";
import { matchParens, tokenLabel, type FormulaToken } from "@/lib/formula-tokens";
import { cn } from "@/lib/utils";
import { KpiBadge } from "./KpiBadge";

export const CANVAS_DROP_ID = "formula-canvas";

/** Bracket colours by nesting depth (700-level on near-white: all above 4.5:1). */
export const DEPTH_TINTS = [
  { fg: "#6d28d9", bg: "#f5f3ff" },
  { fg: "#0369a1", bg: "#f0f9ff" },
  { fg: "#0f766e", bg: "#f0fdfa" },
  { fg: "#be185d", bg: "#fdf2f8" },
];
export const NUMBER_COLOR = "#c2410c";
const ERROR = "var(--rag-poor)";

/**
 * The formula as chips. Between every two chips (and at both ends) sits a gap: the insertion point
 * ("caret") is one of these gaps, and they are the drop targets while dragging. The canvas itself is the
 * single focusable element (its key handling lives in the builder, which owns the state); chips are not
 * tab stops, so a 100-KPI formula is still one Tab away from the rest of the dialog.
 */
export function FormulaCanvas({
  tokens,
  caret,
  kpiByName,
  editingId,
  emptyHint,
  describedBy,
  canvasRef,
  onKeyDown,
  onCaret,
  onRemove,
  onStartEdit,
  onCommitNumber,
  onCancelEdit,
}: {
  tokens: FormulaToken[];
  caret: number;
  kpiByName: Map<string, FormulaKpi>;
  editingId: string | null;
  emptyHint: string;
  describedBy: string;
  canvasRef: RefObject<HTMLDivElement | null>;
  onKeyDown: (e: KeyboardEvent<HTMLDivElement>) => void;
  onCaret: (gap: number) => void;
  onRemove: (index: number) => void;
  onStartEdit: (id: string) => void;
  onCommitNumber: (id: string, value: string, refocus: boolean) => void;
  onCancelEdit: (id: string) => void;
}) {
  const parens = useMemo(() => matchParens(tokens), [tokens]);
  const { active, over } = useDndContext();
  const { setNodeRef } = useDroppable({ id: CANVAS_DROP_ID, data: { type: "canvas" } });
  const [focused, setFocused] = useState(false);
  const dropGap = active && over?.data.current?.type === "gap" ? (over.data.current.index as number) : null;

  // The bracket pair next to the caret is highlighted, like an editor's bracket matching.
  const hot = new Set<number>();
  [caret - 1, caret].forEach((i) => {
    const partner = parens.partner.get(i);
    if (partner !== undefined) hot.add(i).add(partner);
  });

  // Keep the caret in view as it moves through a long formula.
  useEffect(() => {
    canvasRef.current?.querySelector(`[data-gap="${caret}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [caret, canvasRef]);

  const gapState = (i: number) =>
    dropGap === i ? "drop" : dropGap === null && caret === i ? (focused ? "caret" : "caret-dim") : "none";

  return (
    <div
      ref={(el) => {
        setNodeRef(el);
        canvasRef.current = el;
      }}
      role="group"
      aria-roledescription="formula editor"
      aria-label="Formula"
      aria-describedby={describedBy}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onFocus={() => setFocused(true)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setFocused(false);
      }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onCaret(tokens.length);
      }}
      className={cn(
        "thin-scrollbar relative flex min-h-28 flex-1 cursor-text flex-wrap content-start items-center gap-y-1.5 overflow-y-auto rounded-xl border bg-solid px-2 py-2.5 outline-none transition-[border-color,box-shadow]",
        focused ? "border-[var(--focus)] shadow-[0_0_0_3px_rgba(29,78,216,0.15)]" : "border-hairline",
        active && "border-dashed border-lemon-ink bg-lemon-soft/25",
      )}
    >
      {tokens.length === 0 && (
        <p className="pointer-events-none absolute inset-x-6 top-1/2 -translate-y-1/2 text-center text-sm leading-relaxed text-ink-muted">
          {emptyHint}
        </p>
      )}
      {tokens.map((t, i) => (
        <Fragment key={t.id}>
          <Gap index={i} state={gapState(i)} onCaret={onCaret} />
          <Chip
            token={t}
            index={i}
            kpi={t.kind === "kpi" ? kpiByName.get(t.name) : undefined}
            unmatched={parens.unmatched.has(i)}
            depth={parens.depth[i] ?? 0}
            hot={hot.has(i)}
            editing={editingId === t.id}
            onCaret={onCaret}
            onRemove={onRemove}
            onStartEdit={onStartEdit}
            onCommitNumber={onCommitNumber}
            onCancelEdit={onCancelEdit}
          />
        </Fragment>
      ))}
      <Gap index={tokens.length} state={gapState(tokens.length)} onCaret={onCaret} grow />
    </div>
  );
}

const Gap = memo(function Gap({
  index,
  state,
  grow,
  onCaret,
}: {
  index: number;
  state: "none" | "caret" | "caret-dim" | "drop";
  grow?: boolean;
  onCaret: (gap: number) => void;
}) {
  const { setNodeRef } = useDroppable({ id: `gap:${index}`, data: { type: "gap", index } });
  return (
    <span
      ref={setNodeRef}
      data-gap={index}
      aria-hidden
      onMouseDown={(e) => {
        e.stopPropagation();
        onCaret(index);
      }}
      className={cn("relative block min-h-8 w-1.5 shrink-0 self-stretch", grow && "min-w-8 flex-1")}
    >
      <span
        className={cn(
          "pointer-events-none absolute inset-y-0.5 left-[2px] w-0.5 rounded-full",
          state === "drop" && "-left-px w-1 bg-lemon-ink",
          state === "caret" && "formula-caret bg-ink",
          state === "caret-dim" && "bg-ink/25",
        )}
      />
    </span>
  );
});

const Chip = memo(function Chip({
  token,
  index,
  kpi,
  unmatched,
  depth,
  hot,
  editing,
  onCaret,
  onRemove,
  onStartEdit,
  onCommitNumber,
  onCancelEdit,
}: {
  token: FormulaToken;
  index: number;
  kpi: FormulaKpi | undefined;
  unmatched: boolean;
  depth: number;
  hot: boolean;
  editing: boolean;
  onCaret: (gap: number) => void;
  onRemove: (index: number) => void;
  onStartEdit: (id: string) => void;
  onCommitNumber: (id: string, value: string, refocus: boolean) => void;
  onCancelEdit: (id: string) => void;
}) {
  const { setNodeRef, listeners, isDragging } = useDraggable({
    id: `tok:${token.id}`,
    data: { type: "token", id: token.id },
    disabled: editing,
  });

  // A click puts the caret on whichever side of the chip was clicked.
  function place(e: MouseEvent<HTMLElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    onCaret(e.clientX < r.left + r.width / 2 ? index : index + 1);
  }

  const tint = DEPTH_TINTS[depth % DEPTH_TINTS.length];
  let body;
  if (token.kind === "kpi") {
    const known = kpi !== undefined;
    const kpiTintColors = kpiTint(kpi ?? { name: token.name, path: [] });
    body = (
      <span
        className={cn(
          "inline-flex min-w-0 max-w-80 items-center gap-1.5 rounded-lg border bg-solid py-1 pl-1 pr-0.5 text-xs font-medium text-ink shadow-elev-1",
          !known && "border-dashed",
        )}
        style={{ borderColor: known ? `${kpiTintColors.fg}40` : ERROR }}
        title={known ? token.name : `“${token.name}” is not a KPI in this scorecard`}
      >
        <KpiBadge kpi={kpi ?? { name: token.name, path: [] }} size="sm" />
        <span className={cn("truncate", !known && "text-[var(--rag-poor)]")}>{token.name}</span>
        <button
          type="button"
          tabIndex={-1}
          aria-label={`Remove “${token.name}”`}
          onMouseDown={(e) => {
            e.preventDefault();
            e.stopPropagation();
          }}
          onClick={(e) => {
            e.stopPropagation();
            onRemove(index);
          }}
          className="shrink-0 rounded-md p-0.5 text-ink-muted hover:bg-black/5 hover:text-ink"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </span>
    );
  } else if (token.kind === "number") {
    body = editing ? (
      <input
        autoFocus
        defaultValue={token.value}
        inputMode="decimal"
        aria-label="Number"
        size={Math.max(3, token.value.length + 1)}
        onFocus={(e) => e.currentTarget.select()}
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter") {
            e.preventDefault();
            onCommitNumber(token.id, e.currentTarget.value, true);
          } else if (e.key === "Escape") {
            e.preventDefault();
            onCancelEdit(token.id);
          }
        }}
        onBlur={(e) => onCommitNumber(token.id, e.currentTarget.value, false)}
        className="rounded-md border border-[var(--focus)] bg-solid px-1.5 py-0.5 font-mono text-sm font-semibold outline-none"
        style={{ color: NUMBER_COLOR }}
      />
    ) : (
      <span
        className="rounded-md px-1 py-0.5 font-mono text-sm font-semibold hover:bg-[#fff7ed]"
        style={{ color: NUMBER_COLOR }}
        title="Double-click to edit"
      >
        {token.value}
      </span>
    );
  } else if (token.kind === "fn" || token.kind === "open" || token.kind === "close") {
    const color = unmatched ? ERROR : tint.fg;
    body = (
      <span
        className={cn(
          "rounded-lg border px-1.5 py-0.5 font-mono font-semibold transition-shadow",
          token.kind === "fn" ? "text-xs leading-5" : "text-sm leading-5",
          token.kind !== "fn" && !hot && !unmatched && "border-transparent",
        )}
        style={{
          color,
          backgroundColor: unmatched ? "rgba(211,47,47,0.08)" : token.kind === "fn" || hot ? tint.bg : undefined,
          borderColor: unmatched ? ERROR : token.kind === "fn" || hot ? `${color}40` : undefined,
          boxShadow: hot ? `0 0 0 2px ${color}33` : undefined,
        }}
        title={unmatched ? "This bracket has no partner" : undefined}
      >
        {tokenLabel(token)}
      </span>
    );
  } else if (token.kind === "op" || token.kind === "comma") {
    body = <span className="px-1 font-mono text-base font-semibold leading-5 text-ink-muted">{tokenLabel(token)}</span>;
  } else {
    body = <span className="rounded-md bg-black/[0.05] px-1.5 py-0.5 font-mono text-xs text-ink">{token.value}</span>;
  }

  return (
    <span
      ref={setNodeRef}
      {...listeners}
      data-token-index={index}
      onClick={place}
      onDoubleClick={token.kind === "number" && !editing ? () => onStartEdit(token.id) : undefined}
      className={cn(
        "relative inline-flex min-w-0 max-w-full select-none items-center [-webkit-touch-callout:none]",
        !editing && "cursor-grab active:cursor-grabbing",
        isDragging && "opacity-30",
      )}
    >
      {body}
    </span>
  );
});

/** What follows the pointer while dragging: a copy of the chip (or of the KPI being added). */
export function DragChip({ token, kpi }: { token: FormulaToken; kpi?: FormulaKpi }) {
  if (token.kind === "kpi") {
    return (
      <span className="inline-flex max-w-80 cursor-grabbing items-center gap-1.5 rounded-lg border border-lemon-ink/40 bg-solid py-1 pl-1 pr-2 text-xs font-medium text-ink shadow-elev-3">
        <KpiBadge kpi={kpi ?? { name: token.name, path: [] }} size="sm" />
        <span className="truncate">{token.name}</span>
      </span>
    );
  }
  return (
    <span
      className="inline-flex cursor-grabbing items-center rounded-lg border border-lemon-ink/40 bg-solid px-2 py-1 font-mono text-sm font-semibold text-ink shadow-elev-3"
      style={token.kind === "number" ? { color: NUMBER_COLOR } : undefined}
    >
      {tokenLabel(token)}
    </span>
  );
}
