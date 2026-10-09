"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import Link from "next/link";
import { AlertTriangle, ChevronRight, Loader2, RefreshCw, Trash2, Users } from "lucide-react";

import { RagBadge } from "@/components/design-system/RagBadge";
import { SolidPanel } from "@/components/design-system/SolidPanel";
import { showToast, useLiveStatus } from "@/components/live/LiveStatusProvider";
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
import { deleteEvaluation, exportEvaluationsXlsx } from "@/lib/api-client";
import {
  bulkDeleteEvaluations,
  DEFAULT_EVAL_QUERY,
  evalFilterBody,
  fetchEvaluationsPage,
  listScorecardChoices,
  refreshEvaluationRows,
  type EvalPage,
  type EvalQuery,
  type EvalRow,
} from "@/lib/collab-client";
import { saveBlob } from "@/lib/download";
import {
  EMPTY_SELECTION,
  exportableCount,
  exportByFilter,
  exportCap,
  headerState,
  hiddenCount,
  isSelectableRow,
  isSelected,
  mergeRows,
  patchRows,
  pruneIds,
  selectAllMatching,
  selectedCount,
  setRows,
  toggleRow,
  type BulkSelection,
} from "@/lib/eval-bulk";
import { isActiveStatus } from "@/lib/eval-status";
import { usePersistedState } from "@/lib/usePersistedState";
import { cn, formatDateTime } from "@/lib/utils";
import type { EvaluationStatus } from "@/lib/types";
import { EvalFiltersBar, describeQuery } from "./EvalFiltersBar";
import { EvaluationSelectionBar, type ExportStatus } from "./EvaluationSelectionBar";
import { EvaluationStatusBadge } from "./EvaluationStatusBadge";

type Phase = "loading" | "idle" | "more" | "error";

/**
 * The Evaluations list with INFINITE SCROLL. Rows come from `GET /evaluations/page`: server-side keyset pages (stable
 * `(sort key, id)` order, so rows arriving while you scroll never duplicate or skip), every filter applied by the
 * server, and a total count. An IntersectionObserver sentinel loads the next page; changing a filter or the sort
 * resets the list. Selection is either explicit ids or a SERVER-SIDE "all N matching this filter" selection
 * (bulk delete / Excel export resolve it themselves), so it works for thousands of rows that were never loaded.
 * Running rows are refreshed in place by id (progress badges) and new arrivals are announced instead of shifting rows.
 */
export function EvaluationsListClient({
  initialPage,
  initialQuery,
}: {
  initialPage: EvalPage | null;
  initialQuery: EvalQuery;
}) {
  const [query, setQuery] = useState<EvalQuery>(initialQuery);
  const [searchText, setSearchText] = useState(initialQuery.q);
  const [rows, setRows_] = useState<EvalRow[]>(initialPage?.items ?? []);
  const [cursor, setCursor] = useState<string | null>(initialPage?.nextCursor ?? null);
  const [counts, setCounts] = useState({
    total: initialPage?.total ?? 0,
    selectable: initialPage?.selectable ?? 0,
    exportable: initialPage?.exportable ?? 0,
    capped: initialPage?.totalCapped ?? false,
    cap: initialPage?.countCap ?? 10000,
  });
  const [phase, setPhase] = useState<Phase>(initialPage ? "idle" : "loading");
  const [error, setError] = useState<string | null>(null);
  const [newCount, setNewCount] = useState(0);
  const [scorecards, setScorecards] = useState<Array<{ id: string; name: string }>>([]);

  const requestId = useRef(0);
  const sentinel = useRef<HTMLDivElement>(null);
  const loadedTotal = useRef(initialPage?.total ?? 0);
  const firstRun = useRef(true);
  const { slots } = useLiveStatus();

  // ---- loading -------------------------------------------------------------------------------------------------
  const loadFirst = useCallback(async (q: EvalQuery) => {
    const mine = ++requestId.current;
    setPhase("loading");
    setError(null);
    try {
      const page = await fetchEvaluationsPage(q, { limit: 40 });
      if (mine !== requestId.current) return; // a newer filter change superseded this answer
      setRows_(page.items);
      setCursor(page.nextCursor);
      setCounts({ total: page.total, selectable: page.selectable, exportable: page.exportable, capped: page.totalCapped, cap: page.countCap });
      loadedTotal.current = page.total;
      setNewCount(0);
      setPhase("idle");
    } catch (err) {
      if (mine !== requestId.current) return;
      setError(err instanceof Error ? err.message : "Could not load evaluations.");
      setPhase("error");
    }
  }, []);

  const loadMore = useCallback(async () => {
    if (!cursor) return;
    const mine = requestId.current;
    setPhase("more");
    try {
      const page = await fetchEvaluationsPage(query, { cursor, limit: 40 });
      if (mine !== requestId.current) return;
      setRows_((prev) => mergeRows(prev, page.items));
      setCursor(page.nextCursor);
      setPhase("idle");
    } catch (err) {
      if (mine !== requestId.current) return;
      setError(err instanceof Error ? err.message : "Could not load more evaluations.");
      setPhase("error");
    }
  }, [cursor, query]);

  // Filters / sort changed -> reset and reload (the server-rendered first page covers the initial query).
  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false;
      if (initialPage) return;
    }
    void loadFirst(query);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, loadFirst]);

  // Typing in the search box applies after a short pause.
  useEffect(() => {
    if (searchText === query.q) return;
    const t = setTimeout(() => setQuery((q) => ({ ...q, q: searchText })), 300);
    return () => clearTimeout(t);
  }, [searchText, query.q]);

  useEffect(() => {
    void listScorecardChoices()
      .then(setScorecards)
      .catch(() => undefined);
  }, []);

  // Keep the address bar in step (bookmarkable), without a navigation.
  useEffect(() => {
    const url = new URL(window.location.href);
    const set = (k: string, v: string | null) => (v ? url.searchParams.set(k, v) : url.searchParams.delete(k));
    set("scorecard", query.scorecardId);
    set("status", query.status === "all" ? null : query.status);
    set("q", query.q.trim() || null);
    set("batch", query.batchId);
    window.history.replaceState(null, "", url.toString());
  }, [query.scorecardId, query.status, query.q, query.batchId]);

  // ---- infinite scroll sentinel --------------------------------------------------------------------------------
  const hasMore = cursor !== null;
  useEffect(() => {
    const el = sentinel.current;
    if (!el || !hasMore || phase !== "idle") return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) void loadMore();
      },
      { rootMargin: "600px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
    // `rows.length` re-arms the observer after each page, so a sentinel that is still on screen loads the next one.
  }, [hasMore, phase, loadMore, rows.length]);

  // ---- live progress + arrivals --------------------------------------------------------------------------------
  const activeIds = useMemo(() => rows.filter((r) => isActiveStatus(r.status as EvaluationStatus)).map((r) => r.id), [rows]);
  const activeRef = useRef(activeIds);
  useEffect(() => {
    activeRef.current = activeIds;
  });
  const queryRef = useRef(query);
  useEffect(() => {
    queryRef.current = query;
  });
  const busyRunner = Boolean(slots.job);
  useEffect(() => {
    const interval = activeIds.length > 0 || busyRunner ? 4000 : 20000;
    const id = setInterval(async () => {
      if (document.hidden) return;
      try {
        const ids = activeRef.current.slice(0, 100);
        if (ids.length) {
          const fresh = await refreshEvaluationRows(ids);
          setRows_((prev) => patchRows(prev, fresh));
          const gone = ids.filter((i) => !fresh.some((f) => f.id === i)); // deleted meanwhile
          if (gone.length) setRows_((prev) => prev.filter((r) => !gone.includes(r.id)));
        }
        const head = await fetchEvaluationsPage(queryRef.current, { limit: 1 });
        setNewCount(Math.max(0, head.total - loadedTotal.current));
        setCounts((c) => ({ ...c, selectable: head.selectable, exportable: head.exportable }));
      } catch {
        /* polling is best effort */
      }
    }, interval);
    return () => clearInterval(id);
  }, [activeIds.length > 0, busyRunner]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- selection ------------------------------------------------------------------------------------------------
  const [selection, setSelection] = useState<BulkSelection>(EMPTY_SELECTION);
  const [includeReasoning, setIncludeReasoning] = usePersistedState("evaluations.export.includeReasoning", true);
  const [exportStatus, setExportStatus] = useState<ExportStatus>({ kind: "idle" });
  const [bulkOpen, setBulkOpen] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const exportAbort = useRef<AbortController | null>(null);
  const headerRef = useRef<HTMLInputElement>(null);
  const anchor = useRef<string | null>(null);

  // A server-side "all matching" selection belongs to ONE filter: a different filter starts a fresh selection.
  const filterKey = JSON.stringify(evalFilterBody(query));
  useEffect(() => {
    setSelection((s) => (s.all ? EMPTY_SELECTION : s));
    anchor.current = null;
  }, [filterKey]);

  const loadedSelectable = useMemo(() => rows.filter(isSelectableRow).map((r) => r.id), [rows]);
  const hState = headerState(selection, loadedSelectable);
  const nSelected = selectedCount(selection, counts.selectable);
  const nExportable = exportableCount(selection, rows, counts.exportable);
  const busyBulk = exportStatus.kind === "exporting" || exportStatus.kind === "deleting";

  useEffect(() => {
    if (headerRef.current) headerRef.current.indeterminate = hState === "some";
  }, [hState]);
  useEffect(() => {
    if (exportStatus.kind !== "success") return;
    const t = setTimeout(() => setExportStatus({ kind: "idle" }), 6000);
    return () => clearTimeout(t);
  }, [exportStatus]);
  useEffect(() => () => exportAbort.current?.abort(), []);

  function onRowToggle(id: string, shift: boolean) {
    setSelection((prev) => {
      if (shift && anchor.current) {
        const a = loadedSelectable.indexOf(anchor.current);
        const b = loadedSelectable.indexOf(id);
        if (a !== -1 && b !== -1) {
          const [from, to] = [Math.min(a, b), Math.max(a, b)];
          return setRows(prev, loadedSelectable.slice(from, to + 1), !isSelected(prev, id));
        }
      }
      return toggleRow(prev, id);
    });
    anchor.current = id;
  }

  const scorecardName = (id: string) => scorecards.find((s) => s.id === id)?.name ?? null;
  const filterSummary = describeQuery(query, scorecardName).join(" · ").slice(0, 300) || undefined;

  async function runExport() {
    if (nExportable === 0 || busyBulk) return;
    const controller = new AbortController();
    exportAbort.current = controller;
    const timeout = setTimeout(() => controller.abort(), 120_000);
    setExportStatus({ kind: "exporting" });
    try {
      const target = exportByFilter(selection)
        ? {
            filter: evalFilterBody(query),
            excludeIds: selection.all ? [...selection.excluded] : [],
          }
        : rows.filter((r) => selection.ids.has(r.id)).map((r) => r.id);
      const result = await exportEvaluationsXlsx(target, { includeReasoning, filterSummary, signal: controller.signal });
      saveBlob(result.blob, result.filename);
      const skipped =
        result.skipped > 0 ? ` ${result.skipped} evaluation${result.skipped === 1 ? " was" : "s were"} skipped (deleted or not completed).` : "";
      const message = `Downloaded ${result.filename} (${result.count} evaluation${result.count === 1 ? "" : "s"}).${skipped}`;
      // Only a successful export gets here (a failure jumps to the catch below and keeps the selection so the user can
      // retry). The toast carries the message, so the selection bar goes away with the selection (status back to idle).
      setExportStatus({ kind: "idle" });
      setSelection(EMPTY_SELECTION);
      anchor.current = null;
      showToast({ kind: "success", title: "Export complete", body: message });
    } catch (err) {
      const message = controller.signal.aborted
        ? "The export took too long and was cancelled. Try a smaller selection."
        : err instanceof Error
          ? err.message
          : "Couldn't export the evaluations. Try again.";
      setExportStatus({ kind: "error", message });
    } finally {
      clearTimeout(timeout);
    }
  }

  async function runBulkDelete() {
    setBulkOpen(false);
    if (nSelected === 0 || busyBulk) return;
    setExportStatus({ kind: "deleting", done: 0, total: nSelected });
    try {
      const result = await bulkDeleteEvaluations(
        selection.all
          ? { filter: query, excludeIds: [...selection.excluded] }
          : { ids: [...selection.ids] },
      );
      const gone = new Set(selection.all ? [] : selection.ids);
      setRows_((prev) => (selection.all ? prev.filter((r) => !isSelectableRow(r) || selection.excluded.has(r.id)) : prev.filter((r) => !gone.has(r.id))));
      setSelection(EMPTY_SELECTION);
      setExportStatus({
        kind: "success",
        message:
          `Deleted ${result.deleted} evaluation${result.deleted === 1 ? "" : "s"}.` +
          (result.skipped > 0 ? ` ${result.skipped} still running or unavailable were kept.` : ""),
      });
      await loadFirst(query);
    } catch (err) {
      setExportStatus({ kind: "error", action: "delete", message: err instanceof Error ? err.message : "Couldn't delete the evaluations." });
    }
  }

  // ---- single delete ---------------------------------------------------------------------------------------------
  const [pendingDelete, setPendingDelete] = useState<EvalRow | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteEvaluation(pendingDelete.id);
      setRows_((prev) => prev.filter((r) => r.id !== pendingDelete.id));
      setCounts((c) => ({ ...c, total: Math.max(0, c.total - 1), selectable: Math.max(0, c.selectable - 1) }));
      loadedTotal.current = Math.max(0, loadedTotal.current - 1);
      setSelection((s) => pruneIds(toggleIfSelected(s, pendingDelete.id), new Set(rows.map((r) => r.id))));
      setPendingDelete(null);
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : "Couldn't delete this evaluation. Try again.");
    } finally {
      setDeleting(false);
    }
  }

  const filtered = describeQuery(query, scorecardName).length > 0;
  const showEmpty = phase !== "loading" && rows.length === 0 && phase !== "error";

  return (
    <>
      <EvalFiltersBar
        query={query}
        searchText={searchText}
        onSearchText={setSearchText}
        onChange={setQuery}
        scorecards={scorecards}
        total={phase === "loading" && rows.length === 0 ? null : counts.total}
      />
      {counts.capped && (
        <p className="text-xs text-ink-muted">Counting stops at {counts.cap.toLocaleString()}; narrow the filters for an exact number.</p>
      )}

      {newCount > 0 && (
        <div role="status" className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-lemon-soft px-3 py-2 text-sm text-lemon-ink">
          <span>
            {newCount} new evaluation{newCount === 1 ? "" : "s"} matching these filters.
          </span>
          <Button type="button" size="sm" className="min-h-11 sm:min-h-8" onClick={() => void loadFirst(query)}>
            <RefreshCw aria-hidden /> Show
          </Button>
        </div>
      )}

      {rows.length > 0 && counts.selectable > 0 && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-1 py-0.5 text-xs text-ink-muted">
          <label className="flex min-h-11 cursor-pointer items-center gap-2">
            <input
              ref={headerRef}
              type="checkbox"
              checked={hState === "all" || selection.all}
              aria-checked={hState === "some" ? "mixed" : hState === "all" || selection.all}
              onChange={() =>
                setSelection((prev) => (hState === "all" ? setRows(prev, loadedSelectable, false) : setRows(prev, loadedSelectable, true)))
              }
              disabled={loadedSelectable.length === 0}
              aria-label={`Select the ${loadedSelectable.length} completed or failed evaluations loaded so far`}
              className="size-4 accent-[var(--ink)]"
            />
            <span>Select loaded ({loadedSelectable.length})</span>
          </label>
          {!selection.all && hState === "all" && counts.selectable > loadedSelectable.length && (
            <span aria-live="polite" className="min-w-0">
              {loadedSelectable.length} selected.{" "}
              <button type="button" className="min-h-11 underline underline-offset-2 hover:text-ink sm:min-h-0" onClick={() => setSelection(selectAllMatching())}>
                Select all {counts.selectable.toLocaleString()}
                {counts.capped ? "+" : ""} matching this filter
              </button>
            </span>
          )}
          {selection.all && (
            <span aria-live="polite" className="min-w-0">
              All {nSelected.toLocaleString()} matching evaluations are selected{selection.excluded.size > 0 ? ` (${selection.excluded.size} unticked)` : ""}.{" "}
              <button type="button" className="min-h-11 underline underline-offset-2 hover:text-ink sm:min-h-0" onClick={() => setSelection(EMPTY_SELECTION)}>
                Clear selection
              </button>
            </span>
          )}
        </div>
      )}

      {phase === "loading" && rows.length === 0 && (
        <SolidPanel className="flex items-center gap-2 p-6 text-sm text-ink-muted" aria-busy>
          <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> Loading evaluations…
        </SolidPanel>
      )}

      {showEmpty && (
        <SolidPanel className="p-6 text-sm text-ink-muted" data-testid="eval-empty">
          {filtered ? (
            <>
              No evaluations match these filters.{" "}
              <button
                type="button"
                className="min-h-11 underline underline-offset-2 hover:text-ink"
                onClick={() => {
                  setSearchText("");
                  setQuery({ ...DEFAULT_EVAL_QUERY, batchId: query.batchId, sort: query.sort });
                }}
              >
                Clear filters
              </button>
            </>
          ) : (
            "No evaluations yet."
          )}
        </SolidPanel>
      )}

      {rows.length > 0 && (
        <SolidPanel
          className={cn("divide-y divide-hairline transition-opacity", phase === "loading" && "opacity-60")}
          aria-busy={phase === "loading"}
          data-testid="eval-list"
          onKeyDown={(e: KeyboardEvent<HTMLDivElement>) => {
            if (e.key === "Escape" && nSelected > 0) setSelection(EMPTY_SELECTION);
          }}
        >
          {rows.map((row) => (
            <EvalRowView
              key={row.id}
              row={row}
              selected={isSelected(selection, row.id) && isSelectableRow(row)}
              onToggle={onRowToggle}
              onDelete={(r) => {
                setDeleteError(null);
                setPendingDelete(r);
              }}
            />
          ))}
        </SolidPanel>
      )}

      {/* The sentinel + the four end-of-list states: loading more, error with retry, end, (empty handled above). */}
      <div ref={sentinel} aria-hidden className="h-px" />
      <div className="flex min-h-11 items-center justify-center text-xs text-ink-muted" aria-live="polite" data-testid="eval-footer">
        {phase === "more" && (
          <span className="flex items-center gap-2">
            <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> Loading more…
          </span>
        )}
        {phase === "error" && (
          <span role="alert" className="flex flex-wrap items-center justify-center gap-2 text-[var(--rag-poor)]">
            <AlertTriangle className="size-4" aria-hidden /> {error}
            <Button type="button" variant="outline" size="sm" className="min-h-11" onClick={() => (rows.length === 0 ? void loadFirst(query) : void loadMore())}>
              Retry
            </Button>
          </span>
        )}
        {phase === "idle" && !hasMore && rows.length > 0 && (
          <span data-testid="eval-end">You&apos;ve reached the end · {rows.length.toLocaleString()} shown</span>
        )}
        {phase === "idle" && hasMore && (
          <Button type="button" variant="ghost" size="sm" className="min-h-11" onClick={() => void loadMore()}>
            Load more
          </Button>
        )}
      </div>

      <EvaluationSelectionBar
        total={nSelected}
        exportable={nExportable}
        hidden={hiddenCount(selection, rows.map((r) => r.id))}
        allMatching={selection.all}
        maxExport={exportCap(selection)}
        includeReasoning={includeReasoning}
        onIncludeReasoning={setIncludeReasoning}
        status={exportStatus}
        onExport={runExport}
        onDelete={() => setBulkOpen(true)}
        onClear={() => setSelection(EMPTY_SELECTION)}
        onDeselectHidden={() => setSelection((s) => ({ ...s, ids: new Set([...s.ids].filter((id) => rows.some((r) => r.id === id))) }))}
      />

      <Dialog open={!!pendingDelete} onOpenChange={(open) => !open && !deleting && setPendingDelete(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete “{pendingDelete?.name ?? "this evaluation"}”?</DialogTitle>
            <DialogDescription>This permanently deletes the evaluation and its results. This can&apos;t be undone.</DialogDescription>
          </DialogHeader>
          {deleteError && (
            <p role="alert" className="flex items-start gap-1.5 text-xs text-[var(--rag-poor)]">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              {deleteError}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="ghost" className="min-h-11" onClick={() => setPendingDelete(null)} disabled={deleting}>
              Cancel
            </Button>
            <Button type="button" variant="destructive" className="min-h-11" onClick={confirmDelete} disabled={deleting}>
              {deleting && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={bulkOpen} onOpenChange={setBulkOpen}>
        <DialogContent
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            cancelRef.current?.focus(); // Cancel is the default so Enter/Space can never confirm a destructive delete
          }}
        >
          <BulkDeleteBody
            count={nSelected}
            allMatching={selection.all}
            excluded={selection.excluded.size}
            names={rows.filter((r) => selection.ids.has(r.id)).map((r) => r.name)}
            summary={describeQuery(query, scorecardName)}
            cancelRef={cancelRef}
            onCancel={() => setBulkOpen(false)}
            onConfirm={runBulkDelete}
          />
        </DialogContent>
      </Dialog>
    </>
  );
}

function toggleIfSelected(sel: BulkSelection, id: string): BulkSelection {
  return isSelected(sel, id) && !sel.all ? toggleRow(sel, id) : sel;
}

const EvalRowView = memo(function EvalRowView({
  row,
  selected,
  onToggle,
  onDelete,
}: {
  row: EvalRow;
  selected: boolean;
  onToggle: (id: string, shift: boolean) => void;
  onDelete: (row: EvalRow) => void;
}) {
  const selectable = isSelectableRow(row);
  const status = row.status as EvaluationStatus;
  return (
    <div
      // `content-visibility:auto` lets the browser skip layout/paint of rows far off screen, so thousands of loaded
      // rows stay cheap; the intrinsic size keeps the scrollbar stable.
      className={cn(
        "group relative border-l-4 border-l-transparent [content-visibility:auto] [contain-intrinsic-size:auto_76px]",
        selected && "border-l-[var(--ink)] bg-[var(--lemon-soft)]",
      )}
      data-testid="eval-row"
      data-status={row.status}
    >
      <input
        type="checkbox"
        checked={selected}
        disabled={!selectable}
        onChange={() => undefined}
        onClick={(e) => onToggle(row.id, e.shiftKey)}
        aria-label={`Select evaluation “${row.name}”`}
        title={selectable ? undefined : "Only completed or failed evaluations can be selected"}
        className="absolute left-3 top-1/2 z-10 size-4 -translate-y-1/2 accent-[var(--ink)] disabled:cursor-not-allowed disabled:opacity-40"
      />
      <Link
        href={`/charts/${row.scorecardId}/evaluations/${row.id}`}
        className="flex min-h-[4.25rem] items-center justify-between gap-3 py-4 pl-11 pr-10 transition-colors hover:bg-bg focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--focus)] sm:pr-12"
      >
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <p className="min-w-0 truncate text-sm font-medium text-ink">{row.name}</p>
            {row.domain && <Badge variant="muted" className="shrink-0">{row.domain}</Badge>}
            {row.shared && (
              <Badge variant="soft" className="shrink-0" title="This chart is shared with collaborators">
                <Users className="size-3" aria-hidden /> Shared
              </Badge>
            )}
          </div>
          <p className="mt-0.5 truncate text-xs text-ink-muted" suppressHydrationWarning>
            {[row.subjectName, row.subjectEmail].filter(Boolean).length > 0 && `${[row.subjectName, row.subjectEmail].filter(Boolean).join(" · ")} · `}
            {row.scorecardName} · {row.isMine ? "you" : row.runnerName} · {formatDateTime(row.submittedAt ?? row.createdAt)}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2 sm:gap-3">
          {status === "completed" && row.finalWeightedScore != null ? (
            <RagBadge score={row.finalWeightedScore} size="sm" target={row.targetScore ?? 0} />
          ) : (
            <EvaluationStatusBadge evaluation={{ status, stage: row.stage, errorCode: row.errorCode }} />
          )}
          <ChevronRight className="hidden size-4 text-ink-muted sm:block" aria-hidden />
        </div>
      </Link>
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onDelete(row);
        }}
        aria-label={`Delete evaluation “${row.name}”`}
        title="Delete evaluation"
        className={cn(
          "absolute right-1 top-1/2 flex size-11 -translate-y-1/2 items-center justify-center rounded-full text-ink-muted sm:right-3",
          "opacity-0 transition-opacity hover:bg-[var(--rag-poor)]/10 hover:text-[var(--rag-poor)]",
          "group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100",
          "[@media(hover:none)]:opacity-100",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
        )}
      >
        <Trash2 className="size-3.5" aria-hidden />
      </button>
    </div>
  );
});

function BulkDeleteBody({
  count,
  allMatching,
  excluded,
  names,
  summary,
  cancelRef,
  onCancel,
  onConfirm,
}: {
  count: number;
  allMatching: boolean;
  excluded: number;
  names: string[];
  summary: string[];
  cancelRef: RefObject<HTMLButtonElement | null>;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const shown = names.slice(0, 5);
  const more = Math.max(0, names.length - shown.length);
  return (
    <>
      <DialogHeader>
        <DialogTitle>
          Delete {count.toLocaleString()} evaluation{count === 1 ? "" : "s"}?
        </DialogTitle>
        <DialogDescription>
          This permanently deletes {allMatching ? "every completed or failed evaluation matching the current filters" : "the selected evaluations"} and their
          results. Running evaluations are never deleted. This can&apos;t be undone.
        </DialogDescription>
      </DialogHeader>
      {allMatching ? (
        <div className="text-sm text-ink">
          <p>
            All matching{excluded > 0 ? `, except ${excluded} you unticked` : ""}:
          </p>
          <p className="mt-1 text-xs text-ink-muted">{summary.length ? summary.join(" · ") : "No filters (every evaluation you can see)."}</p>
        </div>
      ) : (
        <ul className="list-disc space-y-0.5 pl-5 text-sm text-ink">
          {shown.map((name, i) => (
            <li key={`${i}-${name}`} className="break-words">
              {name}
            </li>
          ))}
          {more > 0 && <li className="list-none text-ink-muted">…and {more} more</li>}
        </ul>
      )}
      <DialogFooter>
        <Button ref={cancelRef} type="button" variant="ghost" className="min-h-11" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="button" variant="destructive" className="min-h-11" onClick={onConfirm}>
          <Trash2 className="size-3.5" aria-hidden />
          Delete {count.toLocaleString()}
        </Button>
      </DialogFooter>
    </>
  );
}
