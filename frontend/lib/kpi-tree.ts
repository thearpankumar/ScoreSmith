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
 * Roll a leaf-level score map up through the hierarchy: a parent's score is
 * the weight-average of its direct children (sibling weights sum to 100 at
 * each level), recursively, up to the top-level weighted final score.
 * Returns a score for every node id (leaf scores pass through unchanged)
 * plus the overall final weighted score.
 */
export function computeKpiRollup(
  nodes: KpiNode[],
  leafScores: Record<string, number>,
): { finalScore: number; scoreByNodeId: Record<string, number> } {
  const tree = buildKpiTree(nodes);
  const scoreByNodeId: Record<string, number> = {};

  function resolve(node: NestedKpiNode): number {
    let score: number;
    if (node.children.length === 0) {
      score = leafScores[node.id] ?? 0;
    } else {
      const totalWeight = node.children.reduce((sum, c) => sum + c.weight, 0) || 100;
      score = node.children.reduce((sum, c) => sum + (resolve(c) * c.weight) / totalWeight, 0);
    }
    scoreByNodeId[node.id] = Math.round(score * 100) / 100;
    return score;
  }

  const totalRootWeight = tree.reduce((sum, n) => sum + n.weight, 0) || 100;
  const finalScore =
    Math.round(tree.reduce((sum, n) => sum + (resolve(n) * n.weight) / totalRootWeight, 0) * 100) / 100;

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
 * Mirrors the backend's deferred-constraint trigger conceptually: sibling weights must
 * sum to 100 within each parent group (top-level KPIs are siblings of each other too,
 * parentId = null). Surfaced read-only in the Overview tab — Cycle 1 enforces this at the
 * DB layer, not here.
 *
 * A KPI with `includedInScoring === false` (see migration
 * 0005_scoring_formula_and_kpi_flags) is excluded from its sibling group's sum entirely —
 * exactly like the DB trigger (`check_kpi_node_weight_sum`, redefined in that migration).
 * A group with zero INCLUDED siblings is skipped (nothing to check), also matching the
 * trigger's own "zero remaining rows" no-op case.
 */
export function validateSiblingWeights(nodes: KpiNode[]): WeightGroupCheck[] {
  const included = nodes.filter((n) => n.includedInScoring);
  const groups = new Map<string | null, KpiNode[]>();
  included.forEach((n) => {
    const key = n.parentId;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(n);
  });

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const checks: WeightGroupCheck[] = [];
  groups.forEach((siblings, parentId) => {
    const { sum, ok } = siblingWeightSum(siblings.map((n) => n.weight));
    checks.push({
      parentId,
      parentName: parentId ? (byId.get(parentId)?.name ?? "Unknown parent") : "Top level",
      sum,
      ok,
    });
  });
  return checks;
}

/**
 * Exact mirror of the backend's `effective_leaf_weights` (backend/app/ai/judge.py): a
 * leaf's share of the whole scorecard is the product of `weight / 100` along its full
 * root-to-leaf path. For a complete scorecard (every sibling group sums to 100) these
 * sum to 1.0.
 *
 * A leaf with `includedInScoring === false` is OMITTED from the result entirely (still
 * scored/tracked elsewhere — see EvaluateTab — but contributes nothing to, and isn't
 * constrained by, the default weighted-average formula), mirroring the backend exactly.
 */
export function effectiveLeafWeights(nodes: KpiNode[]): Record<string, number> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const memo = new Map<string, number>();
  const weightOf = (id: string, seen: Set<string> = new Set()): number => {
    const cached = memo.get(id);
    if (cached !== undefined) return cached;
    const node = byId.get(id);
    if (!node || seen.has(id)) return 0;
    seen.add(id);
    const own = node.weight / 100;
    const result = node.parentId && byId.has(node.parentId) ? own * weightOf(node.parentId, seen) : own;
    memo.set(id, result);
    return result;
  };
  const out: Record<string, number> = {};
  leafKpiNodes(nodes)
    .filter((leaf) => leaf.includedInScoring)
    .forEach((leaf) => (out[leaf.id] = weightOf(leaf.id)));
  return out;
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
