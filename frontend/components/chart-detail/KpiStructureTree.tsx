"use client";

import { useMemo, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  ChevronsDownUp,
  ChevronsUpDown,
  ListTree,
  Loader2,
  Lock,
  Pencil,
  Plus,
  Save,
  Trash2,
  Undo2,
  XCircle,
} from "lucide-react";

import { SolidPanel } from "@/components/design-system/SolidPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  ApiError,
  clearKpiNodeWeight,
  createKpiNode,
  deleteKpiNode,
  renameKpiNode,
  updateKpiIncludedInScoring,
  updateKpiWeights,
  upsertGuideline,
} from "@/lib/api-client";
import {
  buildKpiTree,
  effectiveLeafWeights,
  guidelineCoverage,
  leafDescendantsWeightSum,
  redistributeWeight,
  siblingWeightSum,
  type NestedKpiNode,
} from "@/lib/kpi-tree";
import { cn } from "@/lib/utils";
import type { Guideline, KpiNode } from "@/lib/types";

/** Edit-mode configuration, decided server-side by OverviewTab (status + ownership). */
export interface StructureEditing {
  versionId: string;
  canEdit: boolean;
  /** Plain-language reason shown when `canEdit` is false. */
  lockedReason?: string;
  /** Evaluations already scored against THIS version (for an in-editor warning). */
  evaluationCount: number;
}

const MAX_LEVEL = 4;
const ROOT = "__root__";

/**
 * The scorecard's full KPI hierarchy on the Overview tab: every node (L1->L4) as an
 * expandable tree row with its own weight, its effective share of the whole scorecard
 * (same root-to-leaf product the backend judge uses), the sibling-weight check for
 * each parent group, and guideline coverage out of the framework's 11 levels. Leaf rows
 * expand further to show their full 0-10 guideline ladder inline.
 *
 * EDIT MODE (when `editing.canEdit`): rename KPIs, rebalance weights, add/delete KPIs and
 * write guideline text — each wired straight to the real CRUD routes (no AI involved).
 * Only LEAF KPIs (no children) are weighted at all — a category/grouping node is purely
 * organizational (name + grouping only) and shows no weight field (see backend migration
 * 0008_category_nodes_no_weight). The DB enforces "every LEAF in the scorecard version
 * sums to 100 together" at commit (no longer per immediate sibling group — see
 * `lib/kpi-tree.ts`), so:
 *  - weight edits are staged locally and validated live, as ONE global set, with the same
 *    `siblingWeightSum` rule the chat live preview uses, saved in one atomic call that is
 *    only enabled once every leaf KPI together is back at exactly 100%;
 *  - a new leaf KPI starts at 0% (or 100% if it's the very first KPI in the scorecard),
 *    then you rebalance;
 *  - deleting a weighted leaf KPI first moves its weight proportionally onto its
 *    immediate siblings (if any).
 *
 * Solid surface (dense data), consistent with KpiTreeTable/GuidelinesMatrix.
 */
export function KpiStructureTree({ kpiNodes, editing }: { kpiNodes: KpiNode[]; editing?: StructureEditing }) {
  const router = useRouter();
  const tree = useMemo(() => buildKpiTree(kpiNodes), [kpiNodes]);
  const effective = useMemo(() => effectiveLeafWeights(kpiNodes), [kpiNodes]);
  const parentIds = useMemo(
    () => kpiNodes.filter((n) => kpiNodes.some((c) => c.parentId === n.id)).map((n) => n.id),
    [kpiNodes],
  );

  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [openLadders, setOpenLadders] = useState<Set<string>>(new Set());

  // --- edit-mode state ---
  const [editMode, setEditMode] = useState(false);
  const [staged, setStaged] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [addingUnder, setAddingUnder] = useState<string | null>(null); // node id or ROOT
  const [newName, setNewName] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<KpiNode | null>(null);

  const canEdit = !!editing?.canEdit;
  const isEditing = canEdit && editMode;

  const toggle = (set: Set<string>, id: string) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  };

  const weightOf = (n: KpiNode): number => (n.id in staged ? staged[n.id] : (n.weight ?? 0));
  const siblingsOf = (parentId: string | null) => kpiNodes.filter((n) => n.parentId === parentId);
  const isLeafNode = (n: KpiNode) => !kpiNodes.some((c) => c.parentId === n.id);

  // Every LEAF KPI in the WHOLE tree is now ONE group that must sum to 100 together (not
  // per immediate parent — see migration 0008_category_nodes_no_weight / lib/kpi-tree.ts).
  // Category/grouping nodes (anything with children) carry no weight and never appear here.
  const globalLeafCheck = useMemo(() => {
    const leaves = kpiNodes.filter((n) => isLeafNode(n) && n.includedInScoring);
    return siblingWeightSum(leaves.map((n) => weightOf(n)));
  }, [staged, kpiNodes]); // eslint-disable-line react-hooks/exhaustive-deps
  const hasStaged = Object.keys(staged).length > 0;
  const allStagedOk = globalLeafCheck.ok;

  async function run(action: () => Promise<void>, successNotice?: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (successNotice) setNotice(successNotice);
      router.refresh();
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setBusy(false);
    }
  }

  function stageWeight(node: KpiNode, raw: string) {
    const n = raw === "" ? NaN : Number(raw);
    setStaged((s) => {
      const next = { ...s };
      if (Number.isFinite(n) && Math.abs(n - (node.weight ?? 0)) < 0.001) delete next[node.id];
      else next[node.id] = Number.isFinite(n) ? Math.min(100, Math.max(0, n)) : NaN;
      return next;
    });
  }

  function saveWeights() {
    const payload = Object.entries(staged).map(([id, weight]) => ({ id, weight: Math.round(weight * 100) / 100 }));
    return run(async () => {
      await updateKpiWeights(payload);
      setStaged({});
    }, "Weights saved.");
  }

  function rename(node: KpiNode, name: string) {
    const trimmed = name.trim();
    if (!trimmed || trimmed === node.name) return;
    return run(() => renameKpiNode(node.id, trimmed), `Renamed to “${trimmed}”.`);
  }

  function toggleIncluded(node: KpiNode) {
    const next = !node.includedInScoring;
    return run(
      () => updateKpiIncludedInScoring(node.id, next),
      next
        ? `“${node.name}” is back in the weighted score — every leaf KPI must sum to 100% again.`
        : `“${node.name}” is now excluded from the weighted score — it's still tracked/scored, but doesn't count toward or constrain the scorecard's 100% total.`,
    );
  }

  function addKpi(parentKey: string) {
    const name = newName.trim();
    if (!name || !editing) return;
    const parent = parentKey === ROOT ? null : (kpiNodes.find((n) => n.id === parentKey) ?? null);
    const siblings = siblingsOf(parent?.id ?? null);
    // The new KPI is always a fresh LEAF (no children yet), so it always gets a real
    // weight — 100% only when it's the very first KPI in an otherwise-empty scorecard,
    // 0% otherwise (every leaf's weight is now a GLOBAL share of the whole scorecard, not
    // just relative to its immediate siblings — see migration 0008_category_nodes_no_weight
    // / lib/kpi-tree.ts), so the user rebalances afterwards either way.
    // Exception: the first child under a currently-weighted LEAF turns that leaf into a
    // category (weightless), so the child inherits its weight — otherwise the global leaf
    // sum would drop and the DB trigger would reject the add.
    const parentWasWeightedLeaf =
      !!parent && siblings.length === 0 && parent.includedInScoring && (parent.weight ?? 0) > 0;
    const weight = kpiNodes.length === 0 ? 100 : parentWasWeightedLeaf ? (parent.weight ?? 0) : 0;
    return run(
      async () => {
        await createKpiNode({
          versionId: editing.versionId,
          parentId: parent?.id ?? null,
          level: parent ? parent.level + 1 : 1,
          name,
          weight,
          displayOrder: siblings.reduce((m, s) => Math.max(m, s.displayOrder), -1) + 1,
        });
        // The parent is a category now — drop its stale weight (the child carries it).
        if (parent && parentWasWeightedLeaf) await clearKpiNodeWeight(parent.id);
        setAddingUnder(null);
        setNewName("");
        if (parent) setCollapsed((s) => new Set([...s].filter((id) => id !== parent.id)));
      },
      parentWasWeightedLeaf
        ? `Added “${name}” at ${weight}% (it took over “${parent?.name}”'s weight — “${parent?.name}” is now a category with no weight of its own).`
        : weight === 0
        ? `Added “${name}” at 0%. Rebalance the leaf weights to give it a share, then add its guidelines.`
        : `Added “${name}” at 100% (it's the only KPI in the scorecard). Add its guidelines so it can be scored.`,
    );
  }

  function confirmDelete(node: KpiNode) {
    const { doomedLeaves, pool, rebalanced } = planDeleteRebalance(node, kpiNodes);
    const needsRebalance = rebalanced.length > 0;
    setDeleteTarget(null);
    return run(
      async () => {
        if (needsRebalance) {
          // Step 1: move the doomed leaves' weight onto the remaining leaves in ONE atomic
          // call (every leaf in the scorecard sums to 100 together — see migration
          // 0008_category_nodes_no_weight), so the delete itself never trips the DB's
          // leaf-weight-sum trigger.
          await updateKpiWeights([...rebalanced, ...doomedLeaves.map((n) => ({ id: n.id, weight: 0 }))]);
        }
        try {
          await deleteKpiNode(node.id);
        } catch (err) {
          if (needsRebalance) {
            // Put the weights back exactly as they were, best-effort.
            try {
              await updateKpiWeights([...pool, ...doomedLeaves].map((n) => ({ id: n.id, weight: n.weight ?? 0 })));
            } catch {
              // ignore — the primary error below is what the user needs to see
            }
          }
          throw err;
        }
      },
      needsRebalance
        ? `Deleted “${node.name}”; its weight was spread across the remaining ${pool.length} leaf KPI${pool.length === 1 ? "" : "s"}.`
        : `Deleted “${node.name}”.`,
    );
  }

  if (kpiNodes.length === 0 && !isEditing) {
    return (
      <SolidPanel className="p-5 text-sm text-ink-muted">
        <p>This scorecard version has no KPIs defined yet.</p>
        {canEdit && (
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={() => setEditMode(true)}>
            <Pencil className="size-3.5" aria-hidden /> Edit structure
          </Button>
        )}
      </SolidPanel>
    );
  }

  function renderAddRow(parentKey: string, depth: number, parent: KpiNode | null) {
    const becomesGroup = parent && siblingsOf(parent.id).length === 0;
    return (
      <li className="border-b border-hairline bg-lemon-soft/40 px-3 py-2.5">
        <form
          className="flex flex-wrap items-center gap-2"
          style={{ paddingLeft: `${depth * 1.25 + 1.75}rem` }}
          onSubmit={(e) => {
            e.preventDefault();
            addKpi(parentKey);
          }}
        >
          <input
            autoFocus
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder={parent ? `New sub-KPI under “${parent.name}”` : "New top-level KPI name"}
            aria-label="New KPI name"
            className={cn(FIELD, "min-w-0 flex-1")}
            disabled={busy}
          />
          <Button type="submit" size="sm" disabled={busy || !newName.trim()}>
            <Plus className="size-3.5" aria-hidden /> Add
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setAddingUnder(null)} disabled={busy}>
            Cancel
          </Button>
          {becomesGroup && (
            <p className="w-full text-xs text-ink-muted">
              “{parent!.name}” will become a grouping KPI — it&apos;s then scored through its sub-KPIs, and its own
              guidelines stop being used.
            </p>
          )}
        </form>
      </li>
    );
  }

  function renderNode(node: NestedKpiNode, depth: number): ReactNode {
    const isLeaf = node.children.length === 0;
    // A top-level (depth 0) grouping node is a CATEGORY (see backend/app/ai/
    // scorecard_builder.py's research fan-out and draft_schema.py's module docstring — a
    // category is a Level-1 KpiDraft/KpiNode with no guidelines of its own, grouping its
    // Level-2 children). Styled distinctly from an ordinary grouping KPI deeper in the
    // tree so the category structure a scorecard was organized into on screen is
    // unambiguous, not just a few pixels of extra indentation.
    const isCategory = depth === 0 && !isLeaf;
    const isCollapsed = collapsed.has(node.id);
    const ladderOpen = openLadders.has(node.id);
    const coverage = guidelineCoverage(node);
    // A category/grouping node carries no weight of its own — this is purely an
    // INFORMATIONAL rollup of what share of the WHOLE scorecard its own leaf descendants
    // currently account for (never required to equal any particular number; only the
    // global leaf-sum check at the top of this panel is a pass/fail constraint — see
    // migration 0008_category_nodes_no_weight / lib/kpi-tree.ts).
    const descendantWeightSum = !isLeaf ? leafDescendantsWeightSum(node, weightOf) : 0;
    const share = isLeaf ? effective[node.id] : undefined;
    const weightChanged = node.id in staged;

    return (
      <li key={node.id}>
        <div
          className={cn(
            "grid items-center gap-x-3 gap-y-1 border-b border-hairline px-3 py-2.5",
            isEditing
              ? "grid-cols-[minmax(0,1fr)_auto_auto] sm:grid-cols-[minmax(0,1fr)_5.5rem_6rem_8.5rem_4.5rem]"
              : "grid-cols-[minmax(0,1fr)_auto] sm:grid-cols-[minmax(0,1fr)_4.5rem_6rem_8.5rem]",
            isCategory ? "bg-lemon-soft/40" : depth === 0 && "bg-bg/50",
          )}
        >
          <div className="flex min-w-0 items-center gap-1.5" style={{ paddingLeft: `${depth * 1.25}rem` }}>
            {isLeaf ? (
              <button
                type="button"
                onClick={() => setOpenLadders((s) => toggle(s, node.id))}
                className={CHEVRON_BTN}
                aria-label={ladderOpen ? `Hide guidelines for ${node.name}` : `Show guidelines for ${node.name}`}
                aria-expanded={ladderOpen}
              >
                <ChevronRight className={cn("size-3.5 transition-transform", ladderOpen && "rotate-90")} />
              </button>
            ) : (
              <button
                type="button"
                onClick={() => setCollapsed((s) => toggle(s, node.id))}
                className={CHEVRON_BTN}
                aria-label={isCollapsed ? `Expand ${node.name}` : `Collapse ${node.name}`}
                aria-expanded={!isCollapsed}
              >
                <ChevronRight className={cn("size-3.5 transition-transform", !isCollapsed && "rotate-90")} />
              </button>
            )}
            {isCategory ? (
              <Badge variant="lemon" className="shrink-0 uppercase tracking-wide">
                Category
              </Badge>
            ) : (
              <Badge variant="muted" className="shrink-0">L{node.level}</Badge>
            )}
            <div className="min-w-0 flex-1">
              {isEditing ? (
                <RenameInput key={`${node.id}:${node.name}`} node={node} disabled={busy} onCommit={(v) => rename(node, v)} />
              ) : (
                <p className={cn("truncate text-sm text-ink", depth === 0 && "font-semibold")}>{node.name}</p>
              )}
              {!isLeaf && (
                <p className="text-xs text-ink-muted">
                  {node.children.length} {isCategory ? "KPI" : "sub-KPI"}
                  {node.children.length === 1 ? "" : "s"} · {Math.round(descendantWeightSum * 100) / 100}% of
                  total (no weight of its own)
                </p>
              )}
            </div>
          </div>

          {isEditing ? (
            isLeaf ? (
              <div className="flex flex-col items-end gap-1">
                <label className="flex items-center justify-end gap-0.5">
                  <span className="sr-only">Weight for {node.name} (percent of the whole scorecard)</span>
                  <input
                    type="number"
                    min={0}
                    max={100}
                    step={0.5}
                    inputMode="decimal"
                    value={Number.isFinite(weightOf(node)) ? weightOf(node) : ""}
                    onChange={(e) => stageWeight(node, e.target.value)}
                    disabled={busy || !node.includedInScoring}
                    className={cn(
                      FIELD,
                      "w-16 text-right tabular-nums",
                      weightChanged && "border-[var(--focus)] bg-lemon-soft/60",
                      !node.includedInScoring && "opacity-50",
                    )}
                  />
                  <span className="text-xs text-ink-muted">%</span>
                </label>
                <button
                  type="button"
                  onClick={() => toggleIncluded(node)}
                  disabled={busy || hasStaged}
                  title={
                    hasStaged
                      ? "Save or reset your weight changes first"
                      : node.includedInScoring
                        ? "Exclude this KPI from the weighted score (it stays tracked/scored, just doesn't count toward the 100% total)"
                        : "Include this KPI back in the weighted score"
                  }
                  className={cn(
                    "rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
                    "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]",
                    node.includedInScoring
                      ? "text-ink-muted hover:bg-black/5 hover:text-ink"
                      : "bg-lemon-soft/60 text-lemon-ink",
                  )}
                >
                  {node.includedInScoring ? "Scored" : "Excluded"}
                </button>
              </div>
            ) : (
              <span className="text-right text-xs italic text-ink-muted" title="Categories are purely organizational — no weight of their own">
                no weight
              </span>
            )
          ) : (
            <div className="flex flex-col items-end gap-0.5">
              {isLeaf ? (
                <span className="text-right text-sm font-medium tabular-nums text-ink" title="Weight — this KPI's share of the whole scorecard">
                  {node.weight ?? 0}%
                </span>
              ) : (
                <span className="text-right text-xs italic text-ink-muted" title="Categories are purely organizational — no weight of their own">
                  —
                </span>
              )}
              {!node.includedInScoring && (
                <span
                  className="text-[10px] font-medium uppercase tracking-wide text-ink-muted"
                  title="Tracked/scored, but excluded from the weighted score and the scorecard's 100% total"
                >
                  Excluded
                </span>
              )}
            </div>
          )}
          <span className="hidden text-right text-xs tabular-nums text-ink-muted sm:block" title="Share of the whole scorecard">
            {share !== undefined
              ? `${(share * 100).toFixed(1)}% of total`
              : !isLeaf
                ? `${Math.round(descendantWeightSum * 100) / 100}% of total`
                : "—"}
          </span>
          <span className="hidden items-center justify-end gap-1 text-xs sm:flex">
            {isLeaf ? (
              coverage.defined === 11 ? (
                <span className="flex items-center gap-1 text-[var(--rag-excellent)]">
                  <CheckCircle2 className="size-3.5" aria-hidden /> 11/11 levels
                </span>
              ) : (
                <span className="flex items-center gap-1 text-[var(--rag-poor)]" title={`Missing levels: ${coverage.missing.join(", ")}`}>
                  <XCircle className="size-3.5" aria-hidden /> {coverage.defined}/11 levels
                </span>
              )
            ) : (
              <span className="text-ink-muted">{isCategory ? "category" : "grouping KPI"}</span>
            )}
          </span>
          {isEditing && (
            <span className="flex items-center justify-end gap-0.5">
              {node.level < MAX_LEVEL && (
                <button
                  type="button"
                  className={ICON_BTN}
                  onClick={() => {
                    setAddingUnder(node.id);
                    setNewName("");
                  }}
                  disabled={busy || hasStaged}
                  title={hasStaged ? "Save or reset your weight changes first" : `Add a sub-KPI under ${node.name}`}
                  aria-label={`Add a sub-KPI under ${node.name}`}
                >
                  <Plus className="size-3.5" />
                </button>
              )}
              <button
                type="button"
                className={cn(ICON_BTN, "hover:text-[var(--rag-poor)]")}
                onClick={() => setDeleteTarget(node)}
                disabled={busy || hasStaged}
                title={hasStaged ? "Save or reset your weight changes first" : `Delete ${node.name}`}
                aria-label={`Delete ${node.name}`}
              >
                <Trash2 className="size-3.5" />
              </button>
            </span>
          )}
        </div>

        {isLeaf && ladderOpen && (
          <GuidelineLadder node={node} depth={depth} editable={isEditing} busy={busy} onSave={run} />
        )}

        {(!isCollapsed || addingUnder === node.id) && (node.children.length > 0 || addingUnder === node.id) && (
          <ul>
            {!isCollapsed && node.children.map((c) => renderNode(c, depth + 1))}
            {isEditing && addingUnder === node.id && renderAddRow(node.id, depth + 1, node)}
          </ul>
        )}
      </li>
    );
  }

  const leafCount = Object.keys(effective).length;
  const deleteInfo = deleteTarget ? describeDelete(deleteTarget, kpiNodes) : null;

  return (
    <SolidPanel className="overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline px-4 py-3">
        <p className="flex items-center gap-2 text-sm font-semibold text-ink">
          <ListTree className="size-4 text-lemon-ink" aria-hidden />
          KPI structure
          {isEditing && <Badge variant="lemon">Editing</Badge>}
          <span className="font-normal text-ink-muted">
            · {kpiNodes.length} KPIs, {leafCount} scored leaves · all leaf KPIs sum to{" "}
            <span className={globalLeafCheck.ok ? "text-[var(--rag-excellent)]" : "text-[var(--rag-poor)]"}>
              {globalLeafCheck.sum}%
            </span>
          </span>
        </p>
        <div className="flex flex-wrap gap-1">
          <Button type="button" variant="ghost" size="sm" onClick={() => setCollapsed(new Set())}>
            <ChevronsUpDown className="size-3.5" aria-hidden /> Expand all
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setCollapsed(new Set(parentIds))}>
            <ChevronsDownUp className="size-3.5" aria-hidden /> Collapse all
          </Button>
          {canEdit && !isEditing && (
            <Button type="button" variant="outline" size="sm" onClick={() => setEditMode(true)}>
              <Pencil className="size-3.5" aria-hidden /> Edit structure
            </Button>
          )}
          {isEditing && (
            <Button
              type="button"
              variant="solid"
              size="sm"
              disabled={busy || hasStaged}
              title={hasStaged ? "Save or reset your weight changes first" : undefined}
              onClick={() => {
                setEditMode(false);
                setAddingUnder(null);
                setNotice(null);
                setError(null);
              }}
            >
              Done editing
            </Button>
          )}
        </div>
      </div>

      {editing && !canEdit && editing.lockedReason && (
        <div className="flex items-start gap-2 border-b border-hairline bg-bg/60 px-4 py-2.5 text-xs text-ink-muted">
          <Lock className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <p>{editing.lockedReason}</p>
        </div>
      )}

      {isEditing && (
        <div className="space-y-1.5 border-b border-hairline bg-lemon-soft/40 px-4 py-2.5 text-xs text-ink">
          <p>
            Changes save straight to this scorecard. Rename a KPI by clicking its name (saved when you press Enter or
            click away). Only leaf KPIs (ones with no sub-KPIs) have a weight — categories are purely organizational.
            Weights are each leaf&apos;s share of the WHOLE scorecard, and are saved together once every leaf KPI
            adds up to exactly 100%. Open a KPI&apos;s arrow to edit its 0–10 guidelines.
          </p>
          {editing && editing.evaluationCount > 0 && (
            <p className="flex items-start gap-1.5 text-[var(--rag-poor)]">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              {editing.evaluationCount} evaluation{editing.evaluationCount === 1 ? " was" : "s were"} already scored
              against this version. Renames and weight changes also change how those results read, and KPIs that
              already have scores can&apos;t be deleted.
            </p>
          )}
        </div>
      )}

      {(error || notice || busy) && (
        <div
          role={error ? "alert" : "status"}
          aria-live="polite"
          className={cn(
            "flex items-start gap-2 border-b border-hairline px-4 py-2.5 text-sm",
            error ? "bg-[var(--rag-poor)]/5 text-ink" : "bg-bg/60 text-ink",
          )}
        >
          {busy ? (
            <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin text-ink-muted" aria-hidden />
          ) : error ? (
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-[var(--rag-poor)]" aria-hidden />
          ) : (
            <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-[var(--rag-excellent)]" aria-hidden />
          )}
          <p>{busy ? "Saving…" : (error ?? notice)}</p>
        </div>
      )}

      <div
        className={cn(
          "hidden gap-x-3 border-b border-hairline bg-bg/60 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-ink-muted sm:grid",
          isEditing
            ? "grid-cols-[minmax(0,1fr)_5.5rem_6rem_8.5rem_4.5rem]"
            : "grid-cols-[minmax(0,1fr)_4.5rem_6rem_8.5rem]",
        )}
      >
        <span className="pl-7">KPI</span>
        <span className="text-right">Weight</span>
        <span className="text-right">Share</span>
        <span className="text-right">Guidelines</span>
        {isEditing && <span className="text-right">Actions</span>}
      </div>
      <ul aria-label="KPI hierarchy">
        {tree.map((n) => renderNode(n, 0))}
        {isEditing && addingUnder === ROOT && renderAddRow(ROOT, 0, null)}
      </ul>

      {isEditing && (
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => {
              setAddingUnder(ROOT);
              setNewName("");
            }}
            disabled={busy || hasStaged}
            title={hasStaged ? "Save or reset your weight changes first" : undefined}
          >
            <Plus className="size-3.5" aria-hidden /> Add top-level KPI
          </Button>
        </div>
      )}

      {isEditing && hasStaged && (
        <div className="sticky bottom-0 z-10 flex flex-wrap items-center justify-between gap-3 border-t border-hairline bg-solid px-4 py-3 shadow-[0_-4px_12px_rgba(0,0,0,0.06)]">
          <div className="min-w-0 space-y-1 text-xs">
            <p className="font-semibold text-ink">Unsaved weight changes</p>
            {/* Every leaf KPI in the whole scorecard is now ONE group (see migration
                0008_category_nodes_no_weight) — a single global check, not one per
                immediate sibling group. */}
            <p className={globalLeafCheck.ok ? "text-[var(--rag-excellent)]" : "text-[var(--rag-poor)]"}>
              All leaf KPIs: {globalLeafCheck.sum}%{" "}
              {globalLeafCheck.ok
                ? "✓"
                : `— ${globalLeafCheck.remaining > 0 ? "add" : "remove"} ${Math.abs(globalLeafCheck.remaining)}% to reach 100%`}
            </p>
          </div>
          <div className="flex gap-2">
            <Button type="button" size="sm" variant="ghost" onClick={() => setStaged({})} disabled={busy}>
              <Undo2 className="size-3.5" aria-hidden /> Reset
            </Button>
            <Button
              type="button"
              size="sm"
              onClick={saveWeights}
              disabled={busy || !allStagedOk}
              title={allStagedOk ? undefined : "Every group you changed must add up to exactly 100%"}
            >
              <Save className="size-3.5" aria-hidden /> Save weights
            </Button>
          </div>
        </div>
      )}

      <Dialog open={!!deleteTarget} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete “{deleteTarget?.name}”?</DialogTitle>
            <DialogDescription>This can&apos;t be undone.</DialogDescription>
          </DialogHeader>
          {deleteInfo && (
            <ul className="list-disc space-y-1.5 pl-5 text-sm text-ink">
              {deleteInfo.descendants > 0 && (
                <li>
                  Its {deleteInfo.descendants} sub-KPI{deleteInfo.descendants === 1 ? "" : "s"} and all their guidelines
                  are deleted too.
                </li>
              )}
              {deleteInfo.rebalanced.length > 0 ? (
                <li>
                  The deleted weight is spread across the remaining leaf KPIs so they still add up to 100%:{" "}
                  {deleteInfo.rebalanced.map((r) => `${r.name} → ${r.weight}%`).join(", ")}. You can adjust these
                  afterwards.
                </li>
              ) : deleteInfo.lastInGroup && deleteTarget?.parentId ? (
                <li>It&apos;s the only KPI in its group, so its parent becomes a directly scored KPI again.</li>
              ) : null}
              <li>If an evaluation has already scored this KPI, the delete is blocked to protect that result.</li>
            </ul>
          )}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setDeleteTarget(null)}>
              Cancel
            </Button>
            <Button type="button" variant="destructive" onClick={() => deleteTarget && confirmDelete(deleteTarget)}>
              <Trash2 className="size-3.5" aria-hidden /> Delete KPI
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </SolidPanel>
  );
}

/**
 * Plans how a delete keeps every included leaf KPI summing to 100 together (the DB trigger
 * is global over leaves — migration 0008_category_nodes_no_weight): the weight of every
 * deleted leaf (the node itself, or all leaves under a deleted category) is spread
 * proportionally over its remaining sibling leaves if it has any, else over every
 * remaining included leaf in the scorecard.
 */
function planDeleteRebalance(node: KpiNode, all: KpiNode[]) {
  const doomed = new Set([node.id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const n of all) {
      if (n.parentId && doomed.has(n.parentId) && !doomed.has(n.id)) {
        doomed.add(n.id);
        grew = true;
      }
    }
  }
  const isLeaf = (n: KpiNode) => !all.some((c) => c.parentId === n.id);
  const doomedLeaves = all.filter((n) => doomed.has(n.id) && isLeaf(n) && n.includedInScoring);
  const freed = doomedLeaves.reduce((sum, n) => sum + (n.weight ?? 0), 0);
  const remainingLeaves = all.filter((n) => !doomed.has(n.id) && isLeaf(n) && n.includedInScoring);
  const siblingLeaves = isLeaf(node)
    ? remainingLeaves.filter((n) => n.parentId === node.parentId)
    : [];
  const pool = siblingLeaves.length > 0 ? siblingLeaves : remainingLeaves;
  const rebalanced =
    freed > 0 && pool.length > 0
      ? redistributeWeight(
          pool.map((s) => ({ id: s.id, weight: s.weight ?? 0 })),
          freed,
        )
      : [];
  return { doomed, doomedLeaves, pool, rebalanced };
}

function describeDelete(node: KpiNode, all: KpiNode[]) {
  const { doomed, rebalanced } = planDeleteRebalance(node, all);
  const byId = new Map(all.map((n) => [n.id, n]));
  const siblings = all.filter((n) => n.parentId === node.parentId && n.id !== node.id);
  return {
    descendants: doomed.size - 1,
    rebalanced: rebalanced.map((r) => ({ name: byId.get(r.id)?.name ?? "?", weight: r.weight })),
    lastInGroup: siblings.length === 0,
  };
}

/** Turns backend 409/404/422 details into something a business user can act on. */
function friendlyError(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.message;
    if (/evaluation_kpi_results/i.test(d)) {
      return "This KPI (or one of its sub-KPIs) has already been scored in an evaluation, so it can't be deleted — that would erase the result. Set its weight to 0% instead, or refine the scorecard into a new version.";
    }
    if (/weight sum|sum to 100/i.test(d)) {
      return "That change would leave a group of sibling KPIs not adding up to 100%, so it wasn't saved. Adjust the weights so each group totals exactly 100%.";
    }
    if (/depth|4 levels/i.test(d)) return "KPIs can only be nested 4 levels deep.";
    if (err.status === 0) return d;
    return `The change couldn't be saved: ${d}`;
  }
  return "Something went wrong saving that change. Please try again.";
}

const CHEVRON_BTN =
  "flex size-5 shrink-0 items-center justify-center rounded text-ink-muted hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]";
const ICON_BTN =
  "rounded-md p-1 text-ink-muted hover:bg-black/5 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)] disabled:cursor-not-allowed disabled:opacity-40";
const FIELD =
  "rounded-md border border-hairline bg-solid px-2 py-1 text-sm text-ink placeholder:text-ink-muted focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)] disabled:cursor-not-allowed disabled:opacity-60";

function RenameInput({
  node,
  disabled,
  onCommit,
}: {
  node: KpiNode;
  disabled: boolean;
  onCommit: (value: string) => void;
}) {
  const [value, setValue] = useState(node.name);
  const commit = () => {
    if (!value.trim()) setValue(node.name);
    else if (value.trim() !== node.name) onCommit(value);
  };
  return (
    <input
      type="text"
      value={value}
      aria-label={`Name of KPI (level ${node.level})`}
      disabled={disabled}
      onChange={(e) => setValue(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          (e.target as HTMLInputElement).blur();
        } else if (e.key === "Escape") {
          setValue(node.name);
        }
      }}
      className={cn(FIELD, "w-full font-medium", node.level === 1 && "font-semibold")}
    />
  );
}

function GuidelineLadder({
  node,
  depth,
  editable,
  busy,
  onSave,
}: {
  node: KpiNode;
  depth: number;
  editable: boolean;
  busy: boolean;
  onSave: (action: () => Promise<void>, notice?: string) => Promise<void>;
}) {
  const byLevel = new Map((node.guidelines ?? []).map((g) => [g.scoreLevel, g]));
  const [editingLevel, setEditingLevel] = useState<number | null>(null);
  return (
    <div className="border-b border-hairline bg-bg/40 px-3 py-3" style={{ paddingLeft: `${depth * 1.25 + 2.5}rem` }}>
      {editable && (
        <p className="mb-2 text-xs text-ink-muted">
          Each score level needs a description of what that score looks like. Click a level to write or change it.
        </p>
      )}
      <ol className="space-y-1">
        {Array.from({ length: 11 }, (_, i) => 10 - i).map((level) => {
          const g = byLevel.get(level);
          if (editable && editingLevel === level) {
            return (
              <GuidelineForm
                key={level}
                level={level}
                guideline={g}
                busy={busy}
                onCancel={() => setEditingLevel(null)}
                onSubmit={(qualitative, quantitative) =>
                  onSave(async () => {
                    await upsertGuideline({
                      nodeId: node.id,
                      guidelineId: g?.id,
                      scoreLevel: level,
                      qualitativeText: qualitative,
                      quantitativeText: quantitative,
                    });
                    setEditingLevel(null);
                  }, `Saved level ${level} guideline for “${node.name}”.`)
                }
              />
            );
          }
          const content = (
            <>
              <span className="font-semibold tabular-nums text-ink">{level}</span>
              <span className={g ? "text-ink" : "italic text-[var(--rag-poor)]"}>
                {g ? g.qualitativeText : editable ? "Not defined — click to add" : "Not defined"}
                {g?.quantitativeCriteria && <span className="ml-1.5 text-ink-muted">· {g.quantitativeCriteria}</span>}
              </span>
            </>
          );
          return (
            <li key={level}>
              {editable ? (
                <button
                  type="button"
                  onClick={() => setEditingLevel(level)}
                  disabled={busy}
                  className="grid w-full grid-cols-[2rem_minmax(0,1fr)_auto] items-start gap-2 rounded-md px-1 py-0.5 text-left text-xs hover:bg-white/70 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]"
                  aria-label={`Edit level ${level} guideline for ${node.name}`}
                >
                  {content}
                  <Pencil className="mt-0.5 size-3 text-ink-muted" aria-hidden />
                </button>
              ) : (
                <div className="grid grid-cols-[2rem_minmax(0,1fr)] gap-2 text-xs">{content}</div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function GuidelineForm({
  level,
  guideline,
  busy,
  onCancel,
  onSubmit,
}: {
  level: number;
  guideline?: Guideline;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (qualitative: string, quantitative: string | undefined) => void;
}) {
  const [qualitative, setQualitative] = useState(guideline?.qualitativeText ?? "");
  const initialQuant = guideline?.quantitativeCriteria ?? "";
  const [quantitative, setQuantitative] = useState(initialQuant);
  return (
    <li className="rounded-lg border border-hairline bg-solid p-2.5">
      <form
        className="space-y-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!qualitative.trim()) return;
          // Only send quantitative criteria when it actually changed, so structured
          // criteria (e.g. a metric threshold) are never overwritten by a text edit.
          onSubmit(qualitative.trim(), quantitative !== initialQuant ? quantitative : undefined);
        }}
      >
        <p className="text-xs font-semibold text-ink">Score {level}</p>
        <label className="block text-xs text-ink-muted">
          What a {level} looks like (required)
          <textarea
            autoFocus
            value={qualitative}
            onChange={(e) => setQualitative(e.target.value)}
            rows={2}
            className={cn(FIELD, "mt-1 block w-full resize-y text-xs")}
            disabled={busy}
          />
        </label>
        <label className="block text-xs text-ink-muted">
          Measurable criteria (optional, e.g. “≤ 2 defects per release”)
          <input
            type="text"
            value={quantitative}
            onChange={(e) => setQuantitative(e.target.value)}
            className={cn(FIELD, "mt-1 block w-full text-xs")}
            disabled={busy}
          />
        </label>
        <div className="flex gap-2">
          <Button type="submit" size="sm" disabled={busy || !qualitative.trim()}>
            <Save className="size-3.5" aria-hidden /> Save level {level}
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        </div>
      </form>
    </li>
  );
}
