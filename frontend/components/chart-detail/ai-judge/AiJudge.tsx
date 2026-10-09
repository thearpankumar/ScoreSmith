"use client";

import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { ChevronDown, FileSpreadsheet, Link2, Loader2, Sparkles, UploadCloud } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { JobBusyNotice, activeJobLink } from "@/components/live/JobBusyNotice";
import { requestLiveRefresh, useLiveStatus } from "@/components/live/LiveStatusProvider";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, createAiJobs } from "@/lib/api-client";
import { parseDriveLinks } from "@/lib/ai-upload";
import { leafKpiNodes } from "@/lib/kpi-tree";
import { cn } from "@/lib/utils";
import type { AiJobItem, Evaluation, KpiNode } from "@/lib/types";
import { BatchRun, SingleRun } from "./AiRunView";
import { DriveTab } from "./DriveTab";
import { KpiPanel } from "./KpiPanel";
import { SheetTab } from "./SheetTab";
import { UploadTab } from "./UploadTab";
import { sheetRowIssue, useBatchSheet } from "./use-batch-sheet";
import { useFileUploads } from "./use-file-uploads";

type InputTab = "upload" | "drive" | "sheet";

const TAB_TRIGGER = "flex-1 gap-1.5 px-2 text-xs sm:flex-none sm:px-4 sm:text-sm";

/** What was queued, shown in place of the form. */
type QueuedRun = { kind: "single"; evaluation: Evaluation } | { kind: "batch"; batchId: string; evaluations: Evaluation[] };

/**
 * The "Ask the AI judge" tab. Shows the input form; once something is queued it is replaced, in the
 * same place, by the live progress of that run (no separate page). "Evaluate another" brings back a
 * fresh form.
 */
export function AiJudge({ scorecardId, kpiNodes }: { scorecardId: string; kpiNodes: KpiNode[] }) {
  const [run, setRun] = useState<QueuedRun | null>(null);
  const [formKey, setFormKey] = useState(0);
  const storageKey = `qs.aiRun.${scorecardId}`;

  // Switching tabs unmounts this view while the evaluation keeps running on the server, so the current run
  // is remembered for the browser session and shown again when the user comes back. Restored after mount
  // (not in the initial state) so the server-rendered HTML and the first client render match.
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(storageKey);
      if (raw) setRun(JSON.parse(raw) as QueuedRun);
    } catch {
      // storage unavailable or corrupt: just show the form
    }
  }, [storageKey]);

  function queued(next: QueuedRun) {
    setRun(next);
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(next));
    } catch {
      // ignore
    }
  }

  function startOver() {
    setRun(null);
    setFormKey((k) => k + 1); // a new key remounts the form with every input cleared
    try {
      sessionStorage.removeItem(storageKey);
    } catch {
      // ignore
    }
  }

  if (run?.kind === "single") return <SingleRun evaluation={run.evaluation} onNew={startOver} />;
  if (run?.kind === "batch") return <BatchRun batchId={run.batchId} initial={run.evaluations} onNew={startOver} />;
  return <AiJudgeForm key={formKey} scorecardId={scorecardId} kpiNodes={kpiNodes} onQueued={queued} />;
}

/**
 * The input form. Three ways to supply the work (files, Drive links, a batch spreadsheet), an optional
 * guidance prompt, then queue. The AI names each evaluation, so there is no name field.
 */
function AiJudgeForm({
  scorecardId,
  kpiNodes,
  onQueued,
}: {
  scorecardId: string;
  kpiNodes: KpiNode[];
  onQueued: (run: QueuedRun) => void;
}) {
  const leaves = useMemo(() => leafKpiNodes(kpiNodes), [kpiNodes]);

  const [tab, setTab] = useState<InputTab>("upload");
  const uploads = useFileUploads("submission");
  const sheetUploads = useFileUploads("batch_sheet", { maxFiles: 1, replace: true });
  const sheetFile = sheetUploads.items[0];
  const sheet = useBatchSheet(sheetFile?.status === "done" ? (sheetFile.s3Key ?? null) : null);
  const [driveText, setDriveText] = useState("");
  const driveEntries = useMemo(() => parseDriveLinks(driveText), [driveText]);

  const [direction, setDirection] = useState("");
  const [showDirection, setShowDirection] = useState(false);
  const directionId = useId();

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blockedLink, setBlockedLink] = useState<string | null>(null);
  // One running evaluation job per user (a batch counts as one): the form is disabled while the slot is busy.
  const { slots } = useLiveStatus();
  const slotBusy = Boolean(slots.job);

  // Per-tab count + validity.
  const driveValid = driveEntries.filter((e) => e.valid);
  const driveHasInvalid = driveEntries.some((e) => e.kind === "invalid");
  const sheetOk = sheet.rows.length > 0 && !sheet.parsing && sheet.rows.every((r) => !sheetRowIssue(r));
  const uploadsOk = uploads.items.length > 0 && uploads.items.every((i) => i.status === "done");

  const { count, valid, why } = (() => {
    if (tab === "upload") {
      return {
        count: uploads.items.length > 0 ? 1 : 0,
        valid: uploadsOk,
        why: uploads.items.length === 0 ? "Add at least one file." : !uploadsOk ? "Wait for every upload to finish (or remove failed files)." : "",
      };
    }
    if (tab === "drive") {
      return {
        count: driveValid.length,
        valid: driveValid.length > 0 && !driveHasInvalid,
        why: driveValid.length === 0 ? "Paste at least one Google Drive link." : driveHasInvalid ? "Fix or remove the invalid links." : "",
      };
    }
    return {
      count: sheet.rows.length,
      valid: sheetOk,
      why: !sheetFile
        ? "Upload a spreadsheet."
        : sheet.parsing || sheetUploads.busy
          ? "Reading the spreadsheet..."
          : sheet.rows.length === 0
            ? "No usable rows found."
            : !sheetOk
              ? "Fix the highlighted rows."
              : "",
    };
  })();

  const label = count <= 1 ? "Evaluate 1 submission" : `Queue ${count} evaluations`;

  function buildItems(): AiJobItem[] {
    if (tab === "upload") {
      return [
        {
          sources: uploads.items.map((i) => ({
            kind: "upload" as const,
            s3Key: i.s3Key as string,
            originalName: i.file.name,
            size: i.file.size,
          })),
        },
      ];
    }
    if (tab === "drive") {
      return driveValid.map((e) => ({ sources: [{ kind: "drive" as const, driveUrl: e.raw }] }));
    }
    return sheet.rows.map((r) => ({
      subjectEmail: r.email.trim() || null,
      subjectName: r.name.trim() || null,
      sources: [{ kind: "drive" as const, driveUrl: r.driveUrl.trim() }],
    }));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!valid || submitting || slotBusy) return;
    setSubmitting(true);
    setError(null);
    setBlockedLink(null);
    try {
      const res = await createAiJobs({ scorecardId, directionPrompt: direction, items: buildItems() });
      requestLiveRefresh(); // the slot is busy from now on: disable the other forms right away
      if (res.batchId && res.evaluations.length > 1) {
        onQueued({ kind: "batch", batchId: res.batchId, evaluations: res.evaluations });
      } else if (res.evaluations[0]) {
        onQueued({ kind: "single", evaluation: res.evaluations[0] });
      } else {
        throw new Error("The evaluation was queued but the server did not return it. Check the Evaluations page.");
      }
    } catch (err) {
      setSubmitting(false);
      if (err instanceof ApiError && err.code === "user_job_active") {
        const ev = typeof err.data?.evaluation_id === "string" ? err.data.evaluation_id : null;
        const sc = typeof err.data?.scorecard_id === "string" ? err.data.scorecard_id : null;
        const batch = typeof err.data?.batch_id === "string" ? err.data.batch_id : null;
        setBlockedLink(activeJobLink({ evaluationId: ev ?? "", batchId: batch, status: "", scorecardId: sc }));
        setError(err.message);
        requestLiveRefresh();
        return;
      }
      setError(err instanceof Error && err.message ? err.message : "Could not queue the evaluation. Please try again.");
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mx-auto grid w-full max-w-6xl grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_20rem] xl:grid-cols-[minmax(0,1fr)_23rem]"
    >
      <GlassCard elevation={1} className="flex min-w-0 flex-col gap-5 p-4 sm:p-6 lg:p-7">
        <div className="flex items-start gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-lemon text-lemon-ink">
            <Sparkles className="size-5" aria-hidden />
          </span>
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-ink">Ask the AI judge</h2>
            <p className="mt-0.5 text-sm text-ink-muted">
              Hand over the work as files, public Google Drive links or a spreadsheet of submissions. The AI reads
              everything, scores every KPI against its guidelines and names each evaluation for you.
            </p>
          </div>
        </div>

        <Tabs value={tab} onValueChange={(v) => setTab(v as InputTab)}>
          <TabsList className="flex h-auto w-full min-h-11 sm:inline-flex sm:w-auto">
            <TabsTrigger value="upload" className={TAB_TRIGGER}>
              <UploadCloud className="size-4" aria-hidden /> Upload files
            </TabsTrigger>
            <TabsTrigger value="drive" className={TAB_TRIGGER}>
              <Link2 className="size-4" aria-hidden /> Drive links
            </TabsTrigger>
            <TabsTrigger value="sheet" className={TAB_TRIGGER}>
              <FileSpreadsheet className="size-4" aria-hidden /> Spreadsheet
            </TabsTrigger>
          </TabsList>
          <TabsContent value="upload">
            <UploadTab uploads={uploads} disabled={submitting} />
          </TabsContent>
          <TabsContent value="drive">
            <DriveTab text={driveText} onChange={setDriveText} entries={driveEntries} disabled={submitting} />
          </TabsContent>
          <TabsContent value="sheet">
            <SheetTab uploads={sheetUploads} sheet={sheet} disabled={submitting} />
          </TabsContent>
        </Tabs>

        <div className="rounded-xl border border-hairline bg-solid/60">
          <button
            type="button"
            onClick={() => setShowDirection((v) => !v)}
            aria-expanded={showDirection}
            aria-controls={directionId}
            className="flex w-full items-center justify-between gap-2 rounded-xl px-4 py-3 text-left text-sm font-medium text-ink focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--focus)]"
          >
            <span>
              Add guidance for the judge <span className="font-normal text-ink-muted">(optional)</span>
            </span>
            <ChevronDown className={cn("size-4 transition-transform", showDirection && "rotate-180")} aria-hidden />
          </button>
          {showDirection && (
            <div id={directionId} className="flex flex-col gap-1.5 px-4 pb-4">
              <Label htmlFor={`${directionId}-text`} className="text-xs text-ink-muted">
                Anything the judge should pay attention to or weigh differently for these submissions
              </Label>
              <Textarea
                id={`${directionId}-text`}
                value={direction}
                onChange={(e) => setDirection(e.target.value)}
                placeholder="e.g. These are hackathon projects: reward working demos over slideware."
                className="min-h-24 bg-solid text-sm"
                maxLength={2000}
                disabled={submitting}
              />
            </div>
          )}
        </div>

        <JobBusyNotice />
        {error && (
          <p className="text-sm text-[var(--rag-poor)]" role="alert">
            {error}{" "}
            {blockedLink && (
              <a href={blockedLink} className="font-semibold underline underline-offset-2">
                View it
              </a>
            )}
          </p>
        )}

        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs text-ink-muted" aria-live="polite">
            {valid
              ? `${leaves.length} KPI${leaves.length === 1 ? "" : "s"} will be scored. You can leave the page; progress is saved.`
              : why}
          </p>
          <Button type="submit" size="lg" disabled={!valid || submitting || leaves.length === 0 || slotBusy} className="w-full sm:w-auto">
            {submitting ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden /> Queuing...
              </>
            ) : (
              label
            )}
          </Button>
        </div>
      </GlassCard>

      <KpiPanel leaves={leaves} />
    </form>
  );
}
