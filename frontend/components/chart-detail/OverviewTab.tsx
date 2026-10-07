"use client";

import type { ReactNode } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, XCircle } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { SolidPanel } from "@/components/design-system/SolidPanel";
import { Badge } from "@/components/ui/badge";
import { ScorecardStatusBadge } from "@/components/charts-library/ScorecardStatusBadge";
import { KpiStructureTree, type StructureEditing } from "./KpiStructureTree";
import { ScoringFormulaPanel } from "./ScoringFormulaPanel";
import { TargetScoreField } from "./TargetScoreField";
import { guidelineCoverage, leafKpiNodes, validateSiblingWeights } from "@/lib/kpi-tree";
import { targetEditPermission } from "@/lib/target-score";
import { formatDate } from "@/lib/utils";
import { updateScoringFormula, validateScoringFormula } from "@/lib/api-client";
import type { Scorecard, ScorecardVersion } from "@/lib/types";

/**
 * Comprehensive scorecard overview: header facts + the FULL KPI hierarchy (every node,
 * weight, effective share, and guideline coverage — see KpiStructureTree) + structural
 * health checks. Previously this tab only showed a handful of summary values and never
 * the KPI tree itself.
 */
export function OverviewTab({
  scorecard,
  version,
  currentUserId,
  evaluationCount,
}: {
  scorecard: Scorecard;
  version: ScorecardVersion;
  /** Dev-auth-stub current user (see api-client getCurrentUserId); null if unresolvable. */
  currentUserId: string | null;
  /** Evaluations scored against THIS version. */
  evaluationCount: number;
}) {
  const router = useRouter();
  const nodes = version.kpiNodes;
  const editing: StructureEditing = {
    versionId: version.id,
    ...structureEditPermission(scorecard, currentUserId),
    evaluationCount,
  };
  const weightChecks = validateSiblingWeights(nodes);
  const allWeightsOk = weightChecks.every((c) => c.ok);
  const leaves = leafKpiNodes(nodes);
  const fullyDefined = leaves.filter((l) => guidelineCoverage(l).defined === 11).length;
  const totalGuidelines = leaves.reduce((s, l) => s + guidelineCoverage(l).defined, 0);
  const maxDepth = nodes.reduce((m, n) => Math.max(m, n.level), 0);
  const levelCounts = [1, 2, 3, 4].map((lvl) => nodes.filter((n) => n.level === lvl).length);

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <GlassCard elevation={1} className="p-5 lg:col-span-2">
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <Badge variant="muted">{scorecard.domain}</Badge>
            <ScorecardStatusBadge status={scorecard.status} />
            <Badge variant="outline">v{version.versionNumber}</Badge>
          </div>

          <Field label="Purpose">{scorecard.purposeStatement || "—"}</Field>
          <Field label="Scope">{scorecard.scope || "—"}</Field>
          <Field label="Version notes">{version.guidelineNotes || "—"}</Field>
        </GlassCard>

        <SolidPanel className="p-5">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">At a glance</p>
          <dl className="mt-3 space-y-2.5 text-sm">
            <Row label="Owner" value={scorecard.ownerName} />
            <div className="flex items-start justify-between gap-2">
              <dt className="pt-1 text-ink-muted">Target score</dt>
              <dd>
                <TargetScoreField
                  scorecardId={scorecard.id}
                  target={scorecard.targetScore}
                  {...targetEditPermission(scorecard, currentUserId)}
                />
              </dd>
            </div>
            <Row
              label="KPIs"
              value={[
                `${leaves.length}`,
                leaves.some((l) => !l.includedInScoring)
                  ? `(${leaves.filter((l) => l.includedInScoring).length} scored, ${leaves.filter((l) => !l.includedInScoring).length} unscored)`
                  : null,
                nodes.length > leaves.length ? `in ${nodes.length - leaves.length} categories` : null,
              ]
                .filter(Boolean)
                .join(" ")}
            />
            <Row
              label="Hierarchy depth"
              value={`${maxDepth} level${maxDepth === 1 ? "" : "s"} (${levelCounts
                .map((c, i) => (c ? `L${i + 1}: ${c}` : null))
                .filter(Boolean)
                .join(", ")})`}
            />
            <Row
              label="Guidelines defined"
              value={`${totalGuidelines} / ${leaves.length * 11} (${fullyDefined}/${leaves.length} KPIs complete)`}
            />
            <Row label="Created" value={formatDate(scorecard.createdAt)} dateValue />
            <Row label="Last updated" value={formatDate(scorecard.updatedAt)} dateValue />
          </dl>
        </SolidPanel>
      </div>

      <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <KpiStructureTree kpiNodes={nodes} editing={editing} />
        <ScoringFormulaPanel
          initialFormula={version.scoringFormula}
          leafKpiNames={leaves.map((l) => l.name)}
          onValidate={(formula) => validateScoringFormula(scorecard.id, version.id, formula)}
          onSave={async (formula) => {
            await updateScoringFormula(scorecard.id, version.id, formula);
            router.refresh();
          }}
        />
      </div>

      <SolidPanel className="p-5">
        {/* Only LEAF KPIs are weighted (categories carry none of their own — see backend
            migration 0008_category_nodes_no_weight), and every leaf in the scorecard sums
            to 100 TOGETHER, not per group — validateSiblingWeights now returns at most one
            entry, kept as a list for a stable shape across both schema generations. */}
        <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-ink-muted">Weight integrity</p>
        <ul className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
          {weightChecks.map((check) => (
            <li key={check.parentId ?? "root"} className="flex items-center justify-between gap-2 text-sm">
              <span className="truncate text-ink-muted">{check.parentName}</span>
              <span
                className={`flex items-center gap-1 font-medium ${check.ok ? "text-[var(--rag-excellent)]" : "text-[var(--rag-poor)]"}`}
              >
                {check.ok ? <CheckCircle2 className="size-3.5" aria-hidden /> : <XCircle className="size-3.5" aria-hidden />}
                {check.sum}%
              </span>
            </li>
          ))}
        </ul>
        {!allWeightsOk && (
          <p className="mt-2 text-xs text-[var(--rag-poor)]">
            Every leaf KPI&apos;s weight should sum to 100% — enforced at the database layer in production.
          </p>
        )}
      </SolidPanel>
    </div>
  );
}

/**
 * Who may edit a saved scorecard's structure directly, and why not otherwise. Mirrors
 * the app's status lifecycle (ScorecardStatusBadge / ScorecardActions):
 *  - only DRAFT scorecards are editable in place — a published scorecard is what
 *    evaluators score against, so it stays fixed (move it back to draft first, or use
 *    "Refine with assistant", which saves the changes as a NEW version instead);
 *  - archived scorecards are read-only until restored;
 *  - only the owner may edit (the backend's dev auth stub has no RBAC yet, so this is
 *    enforced here as a UX rule).
 */
function structureEditPermission(
  scorecard: Scorecard,
  currentUserId: string | null,
): { canEdit: boolean; lockedReason?: string } {
  if (currentUserId && scorecard.ownerId !== currentUserId) {
    return { canEdit: false, lockedReason: `Only the owner, ${scorecard.ownerName}, can edit this scorecard's structure.` };
  }
  if (scorecard.status === "published") {
    return {
      canEdit: false,
      lockedReason:
        "Editing is locked because this scorecard is published — evaluators score against exactly this structure. To change it, use “Move back to draft” at the top of the page, or “Refine with assistant” to save your changes as a new version.",
    };
  }
  if (scorecard.status === "archived") {
    return {
      canEdit: false,
      lockedReason: "This scorecard is archived and read-only. Use “Restore to draft” at the top of the page to edit it again.",
    };
  }
  return { canEdit: true };
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mb-4 last:mb-0">
      <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">{label}</p>
      <p className="mt-1 text-sm leading-relaxed text-ink">{children}</p>
    </div>
  );
}

function Row({ label, value, dateValue }: { label: string; value: string; dateValue?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-ink-muted">{label}</dt>
      {/* dateValue: locale/timezone-formatted text can legitimately differ between the
          server render and the browser — see suppressHydrationWarning usage elsewhere in
          this codebase for the same reason. */}
      <dd className="text-right font-medium text-ink" suppressHydrationWarning={dateValue}>
        {value}
      </dd>
    </div>
  );
}
