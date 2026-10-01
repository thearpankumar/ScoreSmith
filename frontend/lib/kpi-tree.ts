import type { KpiNode } from "./types";

/**
 * Pure helpers for working with the Level1->Level4 KPI hierarchy
 * (materialized server-side as a Postgres `ltree` path; here just a flat
 * array with `parentId` links). Shared by mock-data (to compute rollup
 * scores for sample evaluations) and by client components (the Guidelines
 * matrix, the evaluation result tree table) so there is one definition of
 * "how do sibling weights roll up" across the app.
 */

export interface NestedKpiNode extends KpiNode {
  children: NestedKpiNode[];
}

/** Nest a flat KpiNode[] into a tree using parentId, sorted by displayOrder. */
export function buildKpiTree(nodes: KpiNode[]): NestedKpiNode[] {
  const byId = new Map<string, NestedKpiNode>();
  nodes.forEach((n) => byId.set(n.id, { ...n, children: [] }));
  const roots: NestedKpiNode[] = [];
  byId.forEach((node) => {
    if (node.parentId && byId.has(node.parentId)) {
      byId.get(node.parentId)!.children.push(node);
    } else {
      roots.push(node);
    }
  });
  const sortRec = (list: NestedKpiNode[]) => {
    list.sort((a, b) => a.displayOrder - b.displayOrder);
    list.forEach((n) => sortRec(n.children));
  };
  sortRec(roots);
  return roots;
}

/** Only leaf nodes are directly scored (this is where `guidelines` live). */
export function leafKpiNodes(nodes: KpiNode[]): KpiNode[] {
  const parentIds = new Set(nodes.map((n) => n.parentId).filter(Boolean));
  return nodes.filter((n) => !parentIds.has(n.id));
}

/**
 * Roll a leaf-level score map up through the hierarchy. Only LEAF nodes carry a weight
 * (see `effectiveLeafWeights` below / backend migration 0008_category_nodes_no_weight) —
 * a category/grouping node's own displayed "rolled up" score is simply the
 * weight-weighted average of its OWN leaf descendants' scores, using those leaves'
 * EFFECTIVE (already-global) weights as relative proportions within that subtree. This is
 * purely a DISPLAY computation (never required to be any particular value) — the
 * `finalScore` for the whole scorecard is computed the same way `effectiveLeafWeights`/
 * `computeWeightedFinalScore` already do it: a flat sum over every leaf.
 */
export function computeKpiRollup(
  nodes: KpiNode[],
  leafScores: Record<string, number>,
): { finalScore: number; scoreByNodeId: Record<string, number> } {
  const tree = buildKpiTree(nodes);
  const scoreByNodeId: Record<string, number> = {};
  const leafWeights = effectiveLeafWeights(nodes); // leaf id -> weight/100 (0 if excluded)

  function collectLeaves(node: NestedKpiNode, acc: NestedKpiNode[]): void {
    if (node.children.length === 0) acc.push(node);
    else node.children.forEach((c) => collectLeaves(c, acc));
  }

  function resolve(node: NestedKpiNode): number {
    const leaves: NestedKpiNode[] = [];
    collectLeaves(node, leaves);
    const totalWeight = leaves.reduce((sum, leaf) => sum + (leafWeights[leaf.id] ?? 0), 0);
    const score =
      totalWeight > 0
        ? leaves.reduce((sum, leaf) => sum + (leafScores[leaf.id] ?? 0) * (leafWeights[leaf.id] ?? 0), 0) /
          totalWeight
        : 0;
    scoreByNodeId[node.id] = Math.round(score * 100) / 100;
    return score;
  }

  tree.forEach((root) => resolve(root));
  const finalScore = Math.round(computeWeightedFinalScore(nodes, leafScores) * 100) / 100;

  return { finalScore, scoreByNodeId };
}

/**
 * The ONE sibling-sum rule used across the app — the chat live preview's Σ indicator
 * (LivePreviewPanel), the Overview tab's integrity panel (validateSiblingWeights) and the
 * Overview structure editor (KpiStructureTree) all call this, so "does this group add up"
 * can never disagree between screens. Mirrors the backend trigger's tolerance (0.01).
 * Non-finite weights (e.g. a half-typed number input) count as 0.
 */
export function siblingWeightSum(weights: number[]): { sum: number; ok: boolean; remaining: number } {
  const raw = weights.reduce((s, w) => s + (Number.isFinite(w) ? w : 0), 0);
  const sum = Math.round(raw * 100) / 100;
  return { sum, ok: Math.abs(sum - 100) < 0.01, remaining: Math.round((100 - sum) * 100) / 100 };
}

/**
 * Spread `freed` weight points across `siblings` proportionally to their current
 * weights (evenly if they're all 0), rounded to 2 decimals with the rounding remainder
 * put on the largest sibling so the group stays exactly at its previous total. Used when
 * a KPI is deleted so the remaining group still sums to 100.
 */
export function redistributeWeight(
  siblings: Array<{ id: string; weight: number }>,
  freed: number,
): Array<{ id: string; weight: number }> {
  if (siblings.length === 0) return [];
  const total = siblings.reduce((s, n) => s + n.weight, 0);
  const next = siblings.map((n) => ({
    id: n.id,
    weight: Math.round((n.weight + (total > 0 ? (freed * n.weight) / total : freed / siblings.length)) * 100) / 100,
  }));
  const target = Math.round((total + freed) * 100) / 100;
  const drift = Math.round((target - next.reduce((s, n) => s + n.weight, 0)) * 100) / 100;
  if (drift !== 0) {
    const largest = next.reduce((a, b) => (b.weight > a.weight ? b : a));
    largest.weight = Math.round((largest.weight + drift) * 100) / 100;
  }
  return next;
}

export interface WeightGroupCheck {
  parentId: string | null;
  parentName: string;
  sum: number;
  ok: boolean;
}

/**
 * Mirrors the backend's deferred-constraint trigger (`check_kpi_node_weight_sum`, see
 * migration 0008_category_nodes_no_weight): every LEAF `kpi_node` (no children of its
 * own) in the scorecard must have weights summing to 100 TOGETHER — not per immediate
 * parent group. A category/grouping node (anything with children, at any depth) carries
 * no weight of its own and never participates. Surfaced read-only in the Overview tab —
 * the DB enforces this at commit, not here.
 *
 * A KPI with `includedInScoring === false` (see migration
 * 0005_scoring_formula_and_kpi_flags) is excluded from the sum entirely — exactly like
 * the DB trigger. Returns an empty array when there are no included leaves (nothing to
 * check), matching the trigger's own "zero remaining rows" no-op case. Always at most one
 * entry (there is only ever ONE group now), kept as an array so existing callers that
 * iterate over `WeightGroupCheck[]` don't need special-casing.
 */
export function validateSiblingWeights(nodes: KpiNode[]): WeightGroupCheck[] {
  const leaves = leafKpiNodes(nodes).filter((n) => n.includedInScoring);
  if (leaves.length === 0) return [];
  const { sum, ok } = siblingWeightSum(leaves.map((n) => n.weight ?? 0));
  return [{ parentId: null, parentName: "All leaf KPIs", sum, ok }];
}

/**
 * Exact mirror of the backend's `effective_leaf_weights` (backend/app/ai/judge.py): a
 * leaf's share of the whole scorecard is simply its OWN `weight / 100` — category/
 * grouping nodes (any node with children, at any depth) carry no weight of their own at
 * all (see migration 0008_category_nodes_no_weight), so there is no ancestor chain left
 * to multiply through any more. For a complete scorecard (every leaf sums to 100
 * together) these sum to 1.0.
 *
 * A leaf with `includedInScoring === false` is OMITTED from the result entirely (still
 * scored/tracked elsewhere — see EvaluateTab — but contributes nothing to, and isn't
 * constrained by, the default weighted-average formula), mirroring the backend exactly.
 */
export function effectiveLeafWeights(nodes: KpiNode[]): Record<string, number> {
  const out: Record<string, number> = {};
  leafKpiNodes(nodes)
    .filter((leaf) => leaf.includedInScoring)
    .forEach((leaf) => (out[leaf.id] = (leaf.weight ?? 0) / 100));
  return out;
}

/**
 * Sum of `weightOf(leaf)` over every LEAF descendant of `node` (inclusive of `node`
 * itself if it's already a leaf) — a purely INFORMATIONAL rollup for a category/grouping
 * node's own display row (e.g. "32% of total"), never a pass/fail constraint (only the
 * flat, whole-scorecard leaf sum in `validateSiblingWeights`/`siblingWeightSum` is).
 * `weightOf` defaults to a node's own `.weight` (coalesced to 0) but may be overridden
 * (e.g. by `KpiStructureTree`'s locally-staged, not-yet-saved edits).
 */
export function leafDescendantsWeightSum(
  node: NestedKpiNode,
  weightOf: (n: KpiNode) => number = (n) => n.weight ?? 0,
): number {
  if (node.children.length === 0) return weightOf(node);
  return node.children.reduce((sum, c) => sum + leafDescendantsWeightSum(c, weightOf), 0);
}

/**
 * Exact mirror of the backend's `compute_weighted_score` (judge.py): sum over every leaf
 * of `score * effective_weight`, unclamped and un-normalized, so a client-side preview
 * matches what the backend itself would compute for the same leaf scores. Leaves with
 * no score yet contribute 0.
 */
export function computeWeightedFinalScore(nodes: KpiNode[], leafScores: Record<string, number>): number {
  const weights = effectiveLeafWeights(nodes);
  return Object.entries(weights).reduce((sum, [id, w]) => sum + (leafScores[id] ?? 0) * w, 0);
}

/** How many of the framework's 11 guideline levels (0-10) a leaf KPI defines. */
export function guidelineCoverage(node: KpiNode): { defined: number; total: number; missing: number[] } {
  const levels = new Set((node.guidelines ?? []).map((g) => g.scoreLevel));
  const missing: number[] = [];
  for (let l = 0; l <= 10; l++) if (!levels.has(l)) missing.push(l);
  return { defined: 11 - missing.length, total: 11, missing };
}
