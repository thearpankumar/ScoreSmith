"use client";

import { useState, type ReactNode } from "react";

import { EvaluateTab, EvaluateModeToggle, type EvaluateMode } from "./EvaluateTab";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { KpiNode } from "@/lib/types";

import type { ChartTab } from "./chart-tabs";

/**
 * Client shell for the chart detail tabs. The tab state is lifted here (instead of being
 * uncontrolled inside Radix) so the Evaluate tab's "Score manually / Ask the AI judge"
 * switch can sit on the SAME row as Overview / Guidelines / Evaluate / History rather than
 * stacking a second pill bar underneath. The other tab bodies stay server-rendered and are
 * passed in as nodes.
 */
export function ChartDetailTabs({
  initialTab,
  overview,
  guidelines,
  history,
  scorecardId,
  kpiNodes,
  targetScore,
}: {
  initialTab: ChartTab;
  overview: ReactNode;
  guidelines: ReactNode;
  history: ReactNode;
  scorecardId: string;
  kpiNodes: KpiNode[];
  targetScore?: number;
}) {
  const [tab, setTab] = useState<string>(initialTab);
  const [mode, setMode] = useState<EvaluateMode>("manual");

  return (
    <Tabs value={tab} onValueChange={setTab}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <TabsList className="max-w-full overflow-x-auto">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="guidelines">Guidelines</TabsTrigger>
          <TabsTrigger value="evaluate">Evaluate</TabsTrigger>
          <TabsTrigger value="history">History</TabsTrigger>
        </TabsList>
        {tab === "evaluate" && <EvaluateModeToggle mode={mode} onChange={setMode} />}
      </div>

      <TabsContent value="overview">{overview}</TabsContent>
      <TabsContent value="guidelines">{guidelines}</TabsContent>
      <TabsContent value="evaluate">
        <EvaluateTab scorecardId={scorecardId} kpiNodes={kpiNodes} targetScore={targetScore} mode={mode} />
      </TabsContent>
      <TabsContent value="history">{history}</TabsContent>
    </Tabs>
  );
}
