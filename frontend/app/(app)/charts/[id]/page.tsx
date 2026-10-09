import { notFound } from "next/navigation";

import { OverviewTab } from "@/components/chart-detail/OverviewTab";
import { GuidelinesMatrix } from "@/components/chart-detail/GuidelinesMatrix";
import { ChartDetailTabs } from "@/components/chart-detail/ChartDetailTabs";
import { CHART_TABS } from "@/components/chart-detail/chart-tabs";
import { HistoryTab } from "@/components/chart-detail/HistoryTab";
import { ScorecardActions } from "@/components/chart-detail/ScorecardActions";
import { ScorecardStatusBadge } from "@/components/charts-library/ScorecardStatusBadge";
import { ActivityPanel } from "@/components/sharing/ActivityPanel";
import { ShareButton } from "@/components/sharing/ShareDialog";
import { Badge } from "@/components/ui/badge";
import {
  getCurrentUser,
  getCurrentUserId,
  getScorecardWithVersion,
  listEvaluations,
  listScorecardVersionNumbers,
} from "@/lib/api-client";
import { guidelineCoverage, leafKpiNodes, validateSiblingWeights } from "@/lib/kpi-tree";

// Forced dynamic: live backend fetch on every request (see lib/api-client.ts apiFetch docstring).
export const dynamic = "force-dynamic";

export default async function ChartDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ tab?: string }>;
}) {
  const { id } = await params;
  const { tab } = await searchParams;
  // `?tab=evaluate` etc. deep-links straight to a tab (e.g. from a "Score it" link).
  const initialTab = CHART_TABS.find((t) => t === tab) ?? "overview";
  const withVersion = await getScorecardWithVersion(id);
  if (!withVersion) notFound();
  const { scorecard, version } = withVersion;

  const [evaluations, currentUserId, me, versionNumbers] = await Promise.all([
    listEvaluations({ scorecardId: id }),
    getCurrentUserId().catch(() => null),
    getCurrentUser().catch(() => null),
    listScorecardVersionNumbers(id).catch(() => ({}) as Record<string, number>),
  ]);
  const versionEvaluationCount = evaluations.filter((e) => e.scorecardVersionId === version.id).length;

  // Structural gaps worth a warning before publishing (shown in ScorecardActions).
  const issues = [
    ...validateSiblingWeights(version.kpiNodes)
      .filter((c) => !c.ok)
      .map((c) => `KPIs under “${c.parentName}” add up to ${c.sum}%, not 100%.`),
    ...leafKpiNodes(version.kpiNodes)
      .filter((l) => guidelineCoverage(l).defined < 11)
      .map((l) => `“${l.name}” has ${guidelineCoverage(l).defined} of 11 guideline levels written.`),
  ];
  if (version.kpiNodes.length === 0) issues.unshift("It has no KPIs yet.");

  return (
    <div className="flex flex-col gap-4 py-2">
      <div className="flex flex-wrap items-start justify-between gap-3 md:pr-14">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-semibold text-ink">{scorecard.name}</h1>
          <ScorecardStatusBadge status={scorecard.status} />
          {scorecard.isShared && (
            <Badge variant="soft">
              Shared{scorecard.myRole === "editor" ? ` · owned by ${scorecard.ownerName}` : ` with ${scorecard.collaboratorCount}`}
            </Badge>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 lg:justify-end">
          <ShareButton
            scorecardId={scorecard.id}
            scorecardName={scorecard.name}
            myRole={scorecard.myRole ?? "owner"}
            isShared={Boolean(scorecard.isShared)}
          />
          <ScorecardActions
            scorecardId={scorecard.id}
            status={scorecard.status}
            issues={issues}
            evaluationCount={versionEvaluationCount}
          />
        </div>
      </div>

      <ChartDetailTabs
        initialTab={initialTab}
        scorecardId={scorecard.id}
        kpiNodes={version.kpiNodes}
        targetScore={scorecard.targetScore}
        overview={
          <OverviewTab
            scorecard={scorecard}
            version={version}
            currentUserId={currentUserId}
            evaluationCount={versionEvaluationCount}
          />
        }
        guidelines={<GuidelinesMatrix kpiNodes={version.kpiNodes} evaluations={evaluations} />}
        history={
          <HistoryTab
            scorecardId={scorecard.id}
            evaluations={evaluations}
            versionNumbers={versionNumbers}
            currentVersionId={version.id}
          />
        }
      />

      {scorecard.isShared && <ActivityPanel scorecardId={scorecard.id} currentUserName={me?.name} />}
    </div>
  );
}
